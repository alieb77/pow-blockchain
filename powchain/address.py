"""Adresses.

Depuis la Partie 3, une adresse EST la clé publique Ed25519 de son
propriétaire, en hexadécimal minuscule : 64 caractères [0-9a-f] = 32 octets.
Le format provisoire de la Partie 1 (chaînes libres comme "alice") n'est
plus accepté : une adresse doit pouvoir vérifier une signature.

Conséquences :
* le validateur retrouve la clé de vérification directement dans
  Transaction.sender, sans champ supplémentaire ;
* envoyer des fonds vers une adresse qui ne correspond à aucune clé privée
  connue est possible (le format est valide) mais ces fonds seraient perdus,
  comme sur toute blockchain.

Le format on-chain lui-même est défini dans keys.py (un seul endroit).

Somme de contrôle d'affichage (Partie 7 : wallet)
-------------------------------------------------
L'adresse inscrite dans une transaction reste le hex brut de 64 caractères :
c'est ce qui est hashé et signé, on n'y touche pas. Mais du hex brut ne
détecte AUCUNE faute de frappe : un caractère changé donne une autre adresse
valide, et les fonds partent dans le vide.

On ajoute donc une COUCHE D'AFFICHAGE, purement côté wallet, inspirée d'EIP-55
(Ethereum) et adaptée à SHA-256 (le projet n'utilise pas Keccak) : on garde
les 64 mêmes caractères, mais on MET EN MAJUSCULE certains chiffres
hexadécimaux selon un hash de l'adresse. La casse encode ainsi une petite
somme de contrôle « gratuite » (même longueur, mêmes caractères), qu'un
lecteur recalcule pour repérer une adresse mal recopiée.

    to_checksummed_address("ab12...")  -> "Ab12..." (forme à partager)
    normalize_address("Ab12...")       -> "ab12..." (forme on-chain, vérifiée)

C'est une somme de contrôle, pas une signature : elle attrape les fautes de
frappe, jamais l'envoi volontaire ou accidentel à une adresse valide mais qui
n'appartient à personne (ces fonds restent perdus).
"""

from .crypto import sha256_hex
from .keys import PUBLIC_KEY_HEX_LENGTH, is_valid_public_key_hex

ADDRESS_LENGTH = PUBLIC_KEY_HEX_LENGTH


def is_valid_address(value: object) -> bool:
    """Vrai si value est une clé publique Ed25519 en hexadécimal minuscule (64 car.)."""
    return is_valid_public_key_hex(value)


def to_checksummed_address(address: str) -> str:
    """Forme à somme de contrôle d'une adresse (hex brut) : mêmes 64 caractères, casse variable.

    Un chiffre hexadécimal lettre (a-f) passe en majuscule là où le nibble
    correspondant du SHA-256 de l'adresse minuscule vaut 8 ou plus. Les
    chiffres 0-9 n'ont pas de majuscule : ils ne portent pas de contrôle,
    mais chaque lettre en porte un demi-bit, ce qui suffit à repérer une
    faute de frappe avec une probabilité écrasante.
    """
    if not is_valid_address(address):
        raise ValueError(f"adresse hexadécimale de {ADDRESS_LENGTH} caractères minuscules attendue")
    digest = sha256_hex(address.encode("ascii"))
    return "".join(
        character.upper() if character in "abcdef" and int(digest[index], 16) >= 8 else character
        for index, character in enumerate(address)
    )


def has_valid_checksum(value: object) -> bool:
    """Vrai si value est une adresse dont la casse encode la bonne somme de contrôle.

    Une adresse tout en minuscules ne porte aucune information de casse : elle
    n'est acceptée que si sa forme à somme de contrôle est elle-même tout en
    minuscules (adresse sans aucune lettre a-f à mettre en majuscule). Un
    wallet affiche donc toujours la forme mixte et exige cette forme mixte à
    la saisie ; voir normalize_address().
    """
    return (
        isinstance(value, str)
        and is_valid_address(value.lower())
        and value == to_checksummed_address(value.lower())
    )


def normalize_address(value: str, *, require_checksum: bool = True) -> str:
    """Ramène une adresse saisie à sa forme on-chain (hex minuscule), en vérifiant la casse.

    require_checksum=True (défaut) : value doit porter une somme de contrôle
    correcte (la forme mixte affichée par le wallet). Une adresse tout en
    minuscules est refusée, car sa casse ne prouve rien.
    require_checksum=False : on accepte n'importe quelle casse et on ne
    vérifie que le format brut (échappatoire explicite, ex. --unchecked).
    Lève ValueError si l'adresse est mal formée ou la somme de contrôle fausse.
    """
    if not isinstance(value, str) or not is_valid_address(value.lower()):
        raise ValueError(f"adresse hexadécimale de {ADDRESS_LENGTH} caractères attendue, reçu {value!r}")
    if require_checksum and not has_valid_checksum(value):
        raise ValueError(
            "somme de contrôle d'adresse invalide : recopiez la forme exacte "
            "(majuscules comprises) affichée par le wallet, ou forcez avec --unchecked"
        )
    return value.lower()
