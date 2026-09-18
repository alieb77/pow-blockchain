"""Wallet : un fichier qui garde des clés privées CHIFFRÉES sous un mot de passe.

Jusqu'ici les clés étaient un pis-aller : `keygen` affichait la graine privée
en clair, et `send --seed-hex` la re-passait en argument de commande (visible
dans l'historique du shell). Un wallet corrige cela : il stocke une ou
plusieurs clés dans un fichier `wallet.json`, chacune chiffrée, et ne les
déverrouille que le temps de signer, à partir d'un mot de passe.

Ce module N'IMPORTE PAS cryptography : il compose les primitives de keys.py
(derive_symmetric_key = scrypt, aead_encrypt / aead_decrypt = AES-256-GCM).
L'invariant « seul keys.py importe cryptography » est préservé.

Modèle
------
* Un wallet a UN mot de passe et UN sel (aléatoire, tiré à la création). Le
  mot de passe + le sel donnent, par scrypt, une CLÉ MAÎTRE de 32 octets.
* Chaque clé du wallet est une entrée {label, address, nonce, ciphertext} :
  la graine de 32 octets, chiffrée sous la clé maître avec un nonce propre.
  L'adresse (publique) est stockée en clair ET liée au chiffré comme donnée
  associée : on ne peut pas recoller un chiffré sous une autre adresse.
* Déverrouiller = refaire scrypt une fois puis déchiffrer l'entrée voulue.
  Un mot de passe erroné fait échouer la vérification du tag AES-GCM : jamais
  de graine fausse rendue en silence. Après déchiffrement, on revérifie que
  la graine redonne bien l'adresse annoncée (défense en profondeur).

Le fichier est écrit de façon atomique (fichier temporaire puis os.replace),
comme le stockage du nœud : jamais un wallet à moitié écrit sur le disque.

Limite honnête : le wallet protège la clé AU REPOS. Un mot de passe faible
reste cassable hors ligne (scrypt ralentit, n'empêche pas), un enregistreur
de frappe capte le mot de passe, et un mot de passe PERDU rend les fonds
définitivement inaccessibles — il n'y a pas de récupération. Sauvegardez la
graine (wallet export) à part.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .address import is_valid_address, to_checksummed_address
from .errors import WalletError
from .keys import (
    AEAD_NONCE_BYTES,
    KDF_SALT_BYTES,
    PRIVATE_KEY_BYTES,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    KeyPair,
    aead_decrypt,
    aead_encrypt,
    derive_symmetric_key,
)

WALLET_VERSION = 1
CIPHER_NAME = "AES-256-GCM"
KDF_NAME = "scrypt"
DEFAULT_WALLET_PATH = "wallet.json"


@dataclass(frozen=True, slots=True)
class WalletEntry:
    """Une clé du wallet : son étiquette, son adresse publique, sa graine chiffrée."""

    label: str
    address: str
    nonce: str  # hexadécimal
    ciphertext: str  # hexadécimal


class Wallet:
    """Trousseau de clés chiffrées, adossé (optionnellement) à un fichier wallet.json."""

    __slots__ = ("_salt", "_kdf", "_entries", "path")

    def __init__(
        self,
        salt: bytes,
        entries: tuple[WalletEntry, ...] = (),
        *,
        kdf: dict | None = None,
        path: Path | str | None = None,
    ) -> None:
        if not isinstance(salt, (bytes, bytearray)) or len(salt) != KDF_SALT_BYTES:
            raise WalletError(f"sel de {KDF_SALT_BYTES} octets attendu")
        self._salt = bytes(salt)
        self._kdf = dict(kdf) if kdf else {"name": KDF_NAME, "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}
        self._entries: dict[str, WalletEntry] = {}
        for entry in entries:
            self._entries[entry.label] = entry
        self.path = Path(path) if path is not None else None

    # ---------------------------------------------------------------- création

    @classmethod
    def create(cls, path: Path | str | None = None) -> "Wallet":
        """Nouveau wallet vide, avec un sel aléatoire tout neuf."""
        return cls(os.urandom(KDF_SALT_BYTES), path=path)

    # ------------------------------------------------------------ lecture seule

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, label: object) -> bool:
        return label in self._entries

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._entries)

    @property
    def entries(self) -> tuple[WalletEntry, ...]:
        return tuple(self._entries.values())

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(entry.address for entry in self._entries.values())

    def entry(self, label: str) -> WalletEntry:
        try:
            return self._entries[label]
        except KeyError:
            raise WalletError(f"aucune clé nommée {label!r} dans le wallet") from None

    def address_of(self, label: str) -> str:
        """Adresse on-chain (hex brut) de la clé label."""
        return self.entry(label).address

    def checksummed_address_of(self, label: str) -> str:
        """Adresse de la clé label sous sa forme à somme de contrôle (à partager)."""
        return to_checksummed_address(self.entry(label).address)

    def label_of_address(self, address: str) -> str | None:
        for entry in self._entries.values():
            if entry.address == address:
                return entry.label
        return None

    def suggest_label(self) -> str:
        """Une étiquette libre du genre 'cle-3', pour les commandes qui n'en donnent pas."""
        index = len(self._entries) + 1
        while f"cle-{index}" in self._entries:
            index += 1
        return f"cle-{index}"

    # ---------------------------------------------------------------- écriture

    def add_key_pair(self, key_pair: KeyPair, password: str, *, label: str) -> WalletEntry:
        """Chiffre la graine de key_pair sous le mot de passe et l'ajoute au wallet."""
        if not isinstance(key_pair, KeyPair):
            raise WalletError(f"KeyPair attendu, reçu {type(key_pair).__name__}")
        label = _require_label(label)
        if label in self._entries:
            raise WalletError(f"étiquette déjà utilisée : {label!r}")
        if self.label_of_address(key_pair.address) is not None:
            raise WalletError(f"cette clé est déjà dans le wallet ({label!r})")
        master_key = self._master_key(password)
        if self._entries:
            self._verify_master_key(master_key)  # même mot de passe que les clés déjà présentes
        seed = bytes.fromhex(key_pair.seed_hex)
        nonce, ciphertext = aead_encrypt(master_key, seed, key_pair.address.encode("ascii"))
        entry = WalletEntry(label, key_pair.address, nonce.hex(), ciphertext.hex())
        self._entries[label] = entry
        return entry

    def generate_key(self, password: str, *, label: str) -> KeyPair:
        """Tire une nouvelle clé aléatoire, l'ajoute au wallet, et la retourne."""
        key_pair = KeyPair.generate()
        self.add_key_pair(key_pair, password, label=label)
        return key_pair

    def import_seed_hex(self, seed_hex: str, password: str, *, label: str) -> KeyPair:
        """Ajoute une clé existante à partir de sa graine hexadécimale (sauvegarde restaurée)."""
        try:
            key_pair = KeyPair.from_seed_hex(seed_hex)
        except ValueError as error:
            raise WalletError(f"graine invalide : {error}") from None
        self.add_key_pair(key_pair, password, label=label)
        return key_pair

    def remove(self, label: str) -> None:
        """Retire une clé du wallet (le fichier chiffré reste la seule copie : à manier avec soin)."""
        self.entry(label)  # lève si absente
        del self._entries[label]

    # ---------------------------------------------------------- déverrouillage

    def key_pair(self, label: str, password: str) -> KeyPair:
        """Déchiffre et reconstruit la KeyPair de label, ou lève WalletError."""
        entry = self.entry(label)
        master_key = self._master_key(password)
        seed = self._decrypt_entry(entry, master_key)
        key_pair = KeyPair.from_seed(seed)
        if key_pair.address != entry.address:
            raise WalletError(f"wallet corrompu : la clé {label!r} ne redonne pas son adresse")
        return key_pair

    def export_seed_hex(self, label: str, password: str) -> str:
        """Révèle la graine privée de label (pour une sauvegarde hors ligne). À manier avec soin."""
        return self.key_pair(label, password).seed_hex

    def verify_password(self, password: str) -> bool:
        """Vrai si password déverrouille le wallet (teste la première clé). Faux sur wallet vide."""
        if not self._entries:
            return False
        try:
            self._decrypt_entry(next(iter(self._entries.values())), self._master_key(password))
        except WalletError:
            return False
        return True

    # ------------------------------------------------------------- persistance

    def to_dict(self) -> dict:
        return {
            "version": WALLET_VERSION,
            "kdf": {**self._kdf, "salt": self._salt.hex()},
            "cipher": CIPHER_NAME,
            "keys": [
                {"label": e.label, "address": e.address, "nonce": e.nonce, "ciphertext": e.ciphertext}
                for e in self._entries.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: object, *, path: Path | str | None = None) -> "Wallet":
        if not isinstance(data, dict):
            raise WalletError("wallet illisible : objet JSON attendu")
        if data.get("version") != WALLET_VERSION:
            raise WalletError(f"version de wallet inconnue : {data.get('version')!r}")
        if data.get("cipher") != CIPHER_NAME:
            raise WalletError(f"chiffrement inconnu : {data.get('cipher')!r}")
        kdf = data.get("kdf")
        if not isinstance(kdf, dict) or kdf.get("name") != KDF_NAME:
            raise WalletError("paramètres de dérivation (kdf) absents ou inconnus")
        salt = _require_hex(kdf.get("salt"), KDF_SALT_BYTES, "sel")
        params = {"name": KDF_NAME}
        for name in ("n", "r", "p"):
            value = kdf.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise WalletError(f"paramètre kdf.{name} invalide : {value!r}")
            params[name] = value
        raw_keys = data.get("keys")
        if not isinstance(raw_keys, list):
            raise WalletError("wallet illisible : 'keys' doit être une liste")
        entries: list[WalletEntry] = []
        seen_labels: set[str] = set()
        seen_addresses: set[str] = set()
        for raw in raw_keys:
            if not isinstance(raw, dict):
                raise WalletError("entrée de clé illisible : objet attendu")
            label = _require_label(raw.get("label"))
            address = raw.get("address")
            if not is_valid_address(address):
                raise WalletError(f"adresse de clé invalide : {address!r}")
            if label in seen_labels:
                raise WalletError(f"étiquette en double dans le wallet : {label!r}")
            if address in seen_addresses:
                raise WalletError(f"adresse en double dans le wallet : {address[:16]}...")
            nonce = _require_hex(raw.get("nonce"), AEAD_NONCE_BYTES, "nonce")
            ciphertext = _require_hex(raw.get("ciphertext"), None, "ciphertext")
            seen_labels.add(label)
            seen_addresses.add(address)
            entries.append(WalletEntry(label, address, nonce.hex(), ciphertext.hex()))
        return cls(salt, tuple(entries), kdf=params, path=path)

    @classmethod
    def load(cls, path: Path | str) -> "Wallet":
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise WalletError(f"wallet introuvable ou illisible : {error}") from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise WalletError(f"wallet corrompu (JSON invalide) : {error}") from None
        return cls.from_dict(data, path=path)

    def save(self, path: Path | str | None = None) -> Path:
        """Écrit le wallet de façon atomique (tmp + os.replace) et retourne le chemin."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise WalletError("aucun chemin de fichier pour enregistrer le wallet")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False
        )
        try:
            with handle as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(handle.name, target)
        except OSError as error:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise WalletError(f"impossible d'écrire le wallet : {error}") from None
        self.path = target
        return target

    # ----------------------------------------------------------------- interne

    def _master_key(self, password: str) -> bytes:
        try:
            return derive_symmetric_key(
                password, self._salt, n=self._kdf["n"], r=self._kdf["r"], p=self._kdf["p"]
            )
        except (TypeError, ValueError) as error:
            raise WalletError(f"dérivation de clé impossible : {error}") from None

    def _verify_master_key(self, master_key: bytes) -> None:
        self._decrypt_entry(next(iter(self._entries.values())), master_key)

    def _decrypt_entry(self, entry: WalletEntry, master_key: bytes) -> bytes:
        try:
            seed = aead_decrypt(
                master_key,
                bytes.fromhex(entry.nonce),
                bytes.fromhex(entry.ciphertext),
                entry.address.encode("ascii"),
            )
        except ValueError:
            raise WalletError("mot de passe erroné (ou wallet corrompu)") from None
        if len(seed) != PRIVATE_KEY_BYTES:
            raise WalletError("wallet corrompu : graine de taille inattendue")
        return seed


def _require_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise WalletError(f"étiquette de clé invalide : {value!r} (1 à 64 caractères non vides)")
    return value


def _require_hex(value: object, byte_length: int | None, field: str) -> bytes:
    if not isinstance(value, str):
        raise WalletError(f"{field} invalide : chaîne hexadécimale attendue")
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise WalletError(f"{field} invalide : hexadécimal attendu") from None
    if byte_length is not None and len(raw) != byte_length:
        raise WalletError(f"{field} invalide : {byte_length} octets attendus")
    if byte_length is None and not raw:
        raise WalletError(f"{field} invalide : vide")
    return raw
