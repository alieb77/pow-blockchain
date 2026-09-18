"""Clés et signatures numériques Ed25519.

C'est le SEUL module du projet qui importe la bibliothèque cryptography
(implémentation auditée, adossée à OpenSSL). Aucun algorithme n'est
réimplémenté ici : ce module encapsule la bibliothèque et fixe les
conventions d'encodage du projet. Changer de bibliothèque ne toucherait que
ce fichier.

Pourquoi Ed25519 ?
* Signatures DÉTERMINISTES : même clé + même message => même signature. Pas
  d'aléa à tirer au moment de signer, donc pas de fuite de clé par mauvais
  aléa (le piège historique d'ECDSA : PlayStation 3, plusieurs wallets).
* Rapide, clés courtes (32 octets), signatures courtes (64 octets).
* Utilisé par SSH, Signal, TLS 1.3, Solana.
Alternative écartée : ECDSA sur secp256k1 (Bitcoin, Ethereum). Fonctionne
aussi, mais exige RFC 6979 pour être déterministe et souffre de malléabilité.

Conventions d'encodage
* clé privée : 32 octets (la « graine »), hexadécimal minuscule (64 car.).
  Ne quitte jamais KeyPair : repr() ne l'affiche pas. À l'étape wallet elle
  sera chiffrée sur disque.
* clé publique : 32 octets bruts, hexadécimal minuscule (64 caractères).
* adresse : la clé publique en hexadécimal, telle quelle. Un validateur
  retrouve donc la clé de vérification directement dans Transaction.sender.
  Alternative écartée : adresse = hash de la clé publique (Bitcoin,
  Ethereum). Plus courte, mais oblige à transporter la clé publique dans
  chaque transaction. Un changement de format d'adresse restera possible
  sans toucher aux signatures.
* signature : 64 octets, hexadécimal minuscule (128 caractères).
* message signé : les 32 octets bruts du hash canonique de la transaction
  (voir transaction.py).
"""

import os
import re

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

PRIVATE_KEY_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64
PUBLIC_KEY_HEX_LENGTH = PUBLIC_KEY_BYTES * 2
SIGNATURE_HEX_LENGTH = SIGNATURE_BYTES * 2

_PUBLIC_KEY_HEX_PATTERN = re.compile(rf"[0-9a-f]{{{PUBLIC_KEY_HEX_LENGTH}}}")
_SIGNATURE_HEX_PATTERN = re.compile(rf"[0-9a-f]{{{SIGNATURE_HEX_LENGTH}}}")


def is_valid_public_key_hex(value: object) -> bool:
    """Vrai si value est une chaîne hexadécimale minuscule de 64 caractères."""
    return isinstance(value, str) and _PUBLIC_KEY_HEX_PATTERN.fullmatch(value) is not None


def is_valid_signature_hex(value: object) -> bool:
    """Vrai si value est une chaîne hexadécimale minuscule de 128 caractères."""
    return isinstance(value, str) and _SIGNATURE_HEX_PATTERN.fullmatch(value) is not None


class KeyPair:
    """Clé privée Ed25519 et sa clé publique, qui sert d'adresse."""

    __slots__ = ("_private_key", "_public_key_hex")

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError(f"Ed25519PrivateKey attendue, reçu {type(private_key).__name__}")
        self._private_key = private_key
        self._public_key_hex = private_key.public_key().public_bytes_raw().hex()

    @classmethod
    def generate(cls) -> "KeyPair":
        """Nouvelle clé tirée du générateur aléatoire cryptographique du système."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        """Reconstruit une clé à partir de ses 32 octets (import de wallet, tests)."""
        if not isinstance(seed, (bytes, bytearray)) or len(seed) != PRIVATE_KEY_BYTES:
            raise ValueError(f"graine de {PRIVATE_KEY_BYTES} octets attendue")
        return cls(Ed25519PrivateKey.from_private_bytes(bytes(seed)))

    @classmethod
    def from_seed_hex(cls, seed_hex: str) -> "KeyPair":
        try:
            seed = bytes.fromhex(seed_hex)
        except (TypeError, ValueError):
            raise ValueError("graine hexadécimale de 64 caractères attendue") from None
        return cls.from_seed(seed)

    @property
    def seed_hex(self) -> str:
        """Clé privée en hexadécimal. À ne jamais afficher ni transmettre."""
        return self._private_key.private_bytes_raw().hex()

    @property
    def public_key_hex(self) -> str:
        return self._public_key_hex

    @property
    def address(self) -> str:
        """Adresse = clé publique en hexadécimal."""
        return self._public_key_hex

    def sign(self, message: bytes) -> bytes:
        """Signature Ed25519 (64 octets) du message."""
        if not isinstance(message, (bytes, bytearray)):
            raise TypeError(f"message en octets attendu, reçu {type(message).__name__}")
        return self._private_key.sign(bytes(message))

    def sign_hex(self, message: bytes) -> str:
        return self.sign(message).hex()

    def __repr__(self) -> str:
        return f"KeyPair(address={self.address[:16]}...)"


def verify_signature(public_key_hex: str, signature_hex: str, message: bytes) -> bool:
    """Vrai si signature_hex est la signature Ed25519 de message par public_key_hex.

    Ne lève jamais : toute entrée mal formée donne False.
    """
    if not is_valid_public_key_hex(public_key_hex) or not is_valid_signature_hex(signature_hex):
        return False
    if not isinstance(message, (bytes, bytearray)):
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), bytes(message))
    except (InvalidSignature, ValueError):
        return False
    return True


# --------------------------------------------------------------------------
# Chiffrement d'un secret par mot de passe (Partie 7 : wallet)
#
# Le wallet doit garder des graines privées sur le disque sans les y écrire en
# clair. Deux primitives suffisent, toutes deux tirées de la bibliothèque
# cryptography (jamais réimplémentées, comme le reste de ce module) :
#
# * une FONCTION DE DÉRIVATION DE CLÉ (scrypt) qui transforme un mot de passe
#   humain en une clé de 32 octets. scrypt est « coûteuse en mémoire » : elle
#   ralentit délibérément chaque essai, ce qui rend une attaque par
#   dictionnaire sur un mot de passe volé bien plus chère qu'un simple hash.
#   Le sel (aléatoire, stocké en clair à côté) fait que deux wallets protégés
#   par le même mot de passe n'ont pas la même clé : pas de table précalculée.
# * un CHIFFREMENT AUTHENTIFIÉ (AES-256-GCM) qui chiffre ET signe : au
#   déchiffrement, un mot de passe erroné ou un octet modifié fait échouer la
#   vérification du tag (InvalidTag) au lieu de rendre des octets faux. On peut
#   lier des données publiques au chiffré (associated_data) : elles ne sont pas
#   chiffrées mais leur altération invalide aussi le tag (le wallet y met
#   l'adresse, pour qu'un chiffré ne puisse pas être recollé sous une autre).
#
# Coût de scrypt : n * r * 128 octets de mémoire. n = 2^15, r = 8 => 32 Mio et
# quelques dizaines de millisecondes par essai. Ces paramètres sont stockés
# avec le sel, donc augmentables plus tard sans casser les anciens fichiers.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
KDF_SALT_BYTES = 16
AEAD_KEY_BYTES = 32  # AES-256
AEAD_NONCE_BYTES = 12  # taille de nonce recommandée pour GCM


def derive_symmetric_key(
    password: str, salt: bytes, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P
) -> bytes:
    """Dérive une clé de 32 octets d'un mot de passe et d'un sel, par scrypt."""
    if not isinstance(password, str):
        raise TypeError(f"mot de passe (str) attendu, reçu {type(password).__name__}")
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != KDF_SALT_BYTES:
        raise ValueError(f"sel de {KDF_SALT_BYTES} octets attendu")
    kdf = Scrypt(salt=bytes(salt), length=AEAD_KEY_BYTES, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def aead_encrypt(key: bytes, plaintext: bytes, associated_data: bytes = b"") -> tuple[bytes, bytes]:
    """Chiffre plaintext sous key (AES-256-GCM) et retourne (nonce aléatoire, chiffré+tag)."""
    _require_key(key)
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError(f"plaintext en octets attendu, reçu {type(plaintext).__name__}")
    if not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data en octets attendu")
    nonce = os.urandom(AEAD_NONCE_BYTES)
    ciphertext = AESGCM(bytes(key)).encrypt(nonce, bytes(plaintext), bytes(associated_data))
    return nonce, ciphertext


def aead_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, associated_data: bytes = b"") -> bytes:
    """Déchiffre et vérifie le tag. Lève ValueError si la clé est fausse ou les données altérées."""
    _require_key(key)
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != AEAD_NONCE_BYTES:
        raise ValueError(f"nonce de {AEAD_NONCE_BYTES} octets attendu")
    if not isinstance(ciphertext, (bytes, bytearray)):
        raise TypeError(f"ciphertext en octets attendu, reçu {type(ciphertext).__name__}")
    if not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError("associated_data en octets attendu")
    try:
        return AESGCM(bytes(key)).decrypt(bytes(nonce), bytes(ciphertext), bytes(associated_data))
    except InvalidTag:
        raise ValueError("déchiffrement impossible : mot de passe erroné ou données altérées") from None


def _require_key(key: object) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != AEAD_KEY_BYTES:
        raise ValueError(f"clé symétrique de {AEAD_KEY_BYTES} octets attendue")
