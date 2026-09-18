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
  comme sur toute blockchain. Un wallet ajoutera une somme de contrôle pour
  détecter les fautes de frappe.

Le format lui-même est défini dans keys.py (un seul endroit).
"""

from .keys import PUBLIC_KEY_HEX_LENGTH, is_valid_public_key_hex

ADDRESS_LENGTH = PUBLIC_KEY_HEX_LENGTH


def is_valid_address(value: object) -> bool:
    """Vrai si value est une clé publique Ed25519 en hexadécimal minuscule (64 car.)."""
    return is_valid_public_key_hex(value)
