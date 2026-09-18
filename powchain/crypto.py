"""Primitives cryptographiques du projet.

Décisions :

* SHA-256 provient exclusivement de hashlib (bibliothèque standard). Aucun
  algorithme cryptographique n'est réimplémenté.
* Un hash est TOUJOURS manipulé sous une seule forme : une chaîne
  hexadécimale en minuscules de 64 caractères (= 32 octets). C'est cette
  forme qui est stockée dans Transaction.hash, Block.hash, Block.prev_hash,
  affichée et comparée. La forme « octets bruts » n'apparaît qu'à l'intérieur
  de la sérialisation canonique (voir serialization.py).
"""

import hashlib
import re

from .errors import SerializationError

HASH_BYTE_LENGTH = 32
HASH_HEX_LENGTH = HASH_BYTE_LENGTH * 2

_HASH_HEX_PATTERN = re.compile(rf"[0-9a-f]{{{HASH_HEX_LENGTH}}}")


def sha256_hex(payload: bytes) -> str:
    """Retourne SHA-256(payload) en hexadécimal minuscule (64 caractères)."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError(f"sha256_hex attend des octets, reçu {type(payload).__name__}")
    return hashlib.sha256(payload).hexdigest()


def is_valid_hash_hex(value: object) -> bool:
    """Vrai si value est une chaîne hexadécimale minuscule de 64 caractères."""
    return isinstance(value, str) and _HASH_HEX_PATTERN.fullmatch(value) is not None


def hash_hex_to_bytes(value: str) -> bytes:
    """Convertit un hash hexadécimal canonique en ses 32 octets bruts."""
    if not is_valid_hash_hex(value):
        raise SerializationError(
            f"hash attendu : {HASH_HEX_LENGTH} caractères hexadécimaux minuscules, reçu {value!r}"
        )
    return bytes.fromhex(value)
