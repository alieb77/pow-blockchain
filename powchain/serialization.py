"""Sérialisation canonique (format « powchain v1 »).

Le hash d'une transaction ou d'un bloc est SHA-256 d'une suite d'octets qui
doit être IDENTIQUE quelle que soit la machine, la version de Python ou
l'ordre dans lequel les objets ont été créés. Ce module est le SEUL endroit
qui définit cette suite d'octets. Il ne dépend jamais de la représentation
mémoire d'un objet (pas de repr(), pas de str(obj), pas de pickle, pas de
dict dont l'ordre serait incertain).

Règles générales du format
--------------------------
Encodage       : les chaînes sont encodées en UTF-8.
Entiers        : non signés, big-endian (octet de poids fort en premier),
                 largeur FIXE : uint32 = 4 octets, uint64 = 8 octets.
                 Entier hors bornes, bool, float, None => SerializationError.
Chaînes        : préfixées de leur longueur en octets UTF-8 (uint32), puis les
                 octets eux-mêmes. Une chaîne vide s'encode 00 00 00 00.
                 Ce préfixe rend l'encodage NON ambigu : "ab"+"c" et "a"+"bc"
                 donnent des octets différents, ce qu'une simple concaténation
                 de textes ne garantirait pas.
Hashes         : 32 octets bruts (décodage de l'hexadécimal), sans préfixe de
                 longueur puisque la taille est fixe.
Listes de hash : uint32 (nombre d'éléments) puis chaque hash sur 32 octets,
                 dans l'ordre de la liste. Liste vide => 00 00 00 00.
None           : jamais accepté, dans aucun champ => SerializationError.
Domaine        : chaque structure commence par une étiquette constante
                 (ex. b"powchain/tx/v1"). Ainsi les octets d'une transaction
                 ne peuvent jamais être confondus avec ceux d'un bloc, et un
                 futur changement de format (v2) produira des hashes distincts.

Ordre des champs
----------------
Transaction : DOMAINE_TX     | sender (str) | recipient (str) | amount (uint64) | data (str)
              | sequence (uint64)
Liste de tx : DOMAINE_TXLIST | n (uint32)   | hash_1 (32 o) | ... | hash_n (32 o)
Bloc        : DOMAINE_BLOCK  | index (uint64) | timestamp (uint64)
              | transactions_hash (32 o) | prev_hash (32 o) | difficulty (uint64)
              | nonce (uint64)

Le nonce est volontairement le DERNIER champ de l'en-tête : le mineur peut
pré-hasher tout ce qui précède une seule fois, puis n'ajouter que les 8 octets
du nonce à chaque essai (voir mining.py). Tout autre ordre l'obligerait à
re-sérialiser l'en-tête complet à chaque tentative.

La signature d'une transaction n'est JAMAIS sérialisée ici : elle est
calculée SUR le hash (voir transaction.py), elle ne peut donc pas en faire
partie.

Historique des formats :
* bloc v1 (Partie 1) sans difficulty -> "powchain/block/v2" (Partie 2) ;
* transaction v1 sans sequence -> "powchain/tx/v2" (Partie 3).
Chaque passage a changé les hashes existants ; c'est le rôle de l'étiquette
de domaine que de rendre cette rupture explicite.

Représentations retenues
------------------------
amount       : entier en unités (voir money.py), encodé uint64.
sequence     : entier >= 0, numéro de séquence anti-rejeu du compte
               expéditeur, encodé uint64.
timestamp    : entier, secondes écoulées depuis 1970-01-01T00:00:00 UTC (temps
               Unix), encodé uint64.
difficulty   : entier >= 1 (voir proof_of_work.py), encodé uint64.
transactions : représentées dans le bloc par transactions_hash = SHA-256 de la
               liste ordonnée de leurs hashes (voir block.py), jamais par la
               sérialisation des objets Transaction eux-mêmes.
"""

from collections.abc import Sequence

from .crypto import hash_hex_to_bytes
from .errors import SerializationError

TRANSACTION_DOMAIN = b"powchain/tx/v2"
TRANSACTION_LIST_DOMAIN = b"powchain/txlist/v1"
BLOCK_DOMAIN = b"powchain/block/v2"

UINT32_BYTES = 4
UINT64_BYTES = 8
UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1


def is_uint64(value: object) -> bool:
    """Vrai si value est un entier (pas un bool) représentable sur 8 octets non signés."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= UINT64_MAX


def _encode_unsigned(value: object, byte_width: int, field_name: str) -> bytes:
    maximum = 2 ** (8 * byte_width) - 1
    if not isinstance(value, int) or isinstance(value, bool):
        raise SerializationError(f"{field_name} : entier attendu, reçu {type(value).__name__}")
    if not 0 <= value <= maximum:
        raise SerializationError(f"{field_name} : doit être entre 0 et {maximum}, reçu {value}")
    return value.to_bytes(byte_width, byteorder="big", signed=False)


def encode_uint32(value: int, field_name: str = "uint32") -> bytes:
    """Entier non signé sur 4 octets big-endian."""
    return _encode_unsigned(value, UINT32_BYTES, field_name)


def encode_uint64(value: int, field_name: str = "uint64") -> bytes:
    """Entier non signé sur 8 octets big-endian."""
    return _encode_unsigned(value, UINT64_BYTES, field_name)


def encode_str(value: str, field_name: str = "str") -> bytes:
    """Chaîne UTF-8 préfixée de sa longueur en octets (uint32)."""
    if not isinstance(value, str):
        raise SerializationError(f"{field_name} : chaîne attendue, reçu {type(value).__name__}")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SerializationError(f"{field_name} : chaîne non encodable en UTF-8 ({error})") from None
    return encode_uint32(len(raw), f"longueur de {field_name}") + raw


def encode_hash(value: str, field_name: str = "hash") -> bytes:
    """Hash hexadécimal canonique -> 32 octets bruts."""
    try:
        return hash_hex_to_bytes(value)
    except SerializationError as error:
        raise SerializationError(f"{field_name} : {error}") from None


def serialize_transaction_fields(
    sender: str, recipient: str, amount: int, data: str, sequence: int
) -> bytes:
    """Octets canoniques d'une transaction (ni le hash ni la signature n'en font partie)."""
    return b"".join(
        (
            TRANSACTION_DOMAIN,
            encode_str(sender, "sender"),
            encode_str(recipient, "recipient"),
            encode_uint64(amount, "amount"),
            encode_str(data, "data"),
            encode_uint64(sequence, "sequence"),
        )
    )


def serialize_transaction_hash_list(transaction_hashes: Sequence[str]) -> bytes:
    """Octets canoniques d'une liste ORDONNÉE de hashes de transactions."""
    if isinstance(transaction_hashes, (str, bytes)) or not isinstance(transaction_hashes, Sequence):
        raise SerializationError("liste de hashes de transactions attendue")
    parts = [TRANSACTION_LIST_DOMAIN, encode_uint32(len(transaction_hashes), "nombre de transactions")]
    for position, transaction_hash in enumerate(transaction_hashes):
        parts.append(encode_hash(transaction_hash, f"hash de la transaction n°{position}"))
    return b"".join(parts)


def serialize_block_header_prefix(
    index: int, timestamp: int, transactions_hash: str, prev_hash: str, difficulty: int
) -> bytes:
    """Octets canoniques de l'en-tête SANS le nonce (partie fixe pendant le minage)."""
    return b"".join(
        (
            BLOCK_DOMAIN,
            encode_uint64(index, "index"),
            encode_uint64(timestamp, "timestamp"),
            encode_hash(transactions_hash, "transactions_hash"),
            encode_hash(prev_hash, "prev_hash"),
            encode_uint64(difficulty, "difficulty"),
        )
    )


def serialize_block_header(
    index: int, timestamp: int, transactions_hash: str, prev_hash: str, difficulty: int, nonce: int
) -> bytes:
    """Octets canoniques de l'en-tête complet (le hash du bloc n'en fait pas partie)."""
    return serialize_block_header_prefix(
        index, timestamp, transactions_hash, prev_hash, difficulty
    ) + encode_uint64(nonce, "nonce")
