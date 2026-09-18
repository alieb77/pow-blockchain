"""Codec : conversion des Transaction et Block vers/depuis des dictionnaires JSON.

Le format canonique (serialization.py) définit les OCTETS QUI SONT HASHÉS. Il
ne transporte ni la signature d'une transaction, ni le contenu des
transactions d'un bloc (seulement leur empreinte). Pour envoyer un bloc à un
autre nœud ou l'écrire sur disque, il faut donc un second format, complet et
lisible : ce module le définit, sous forme de dictionnaires JSON.

Décoder n'est pas valider
-------------------------
transaction_from_dict() et block_from_dict() vérifient uniquement la FORME :
clés présentes (ni plus, ni moins), types corrects, entiers dans les bornes
uint64. Elles reconstruisent l'objet tel quel, y compris son hash et sa
signature stockés. Les règles métier (hash recalculé, signature, preuve de
travail, chaînage) sont vérifiées ensuite par validate_transaction() /
validate_block(), exactement comme pour un objet construit localement. Un
nœud ne fait donc jamais confiance à ce qu'il reçoit : il décode, puis valide.

Le format est strict (clé inconnue => CodecError) pour qu'un message
malformé soit détecté au plus tôt et jamais interprété « à peu près ».

Format
------
    transaction : {"sender", "recipient", "amount", "data", "sequence", "hash", "signature"}
    bloc        : {"index", "timestamp", "transactions": [transaction...], "prev_hash",
                   "difficulty", "nonce", "hash"}

Les entiers sont des entiers JSON (jamais des chaînes ni des flottants), les
hashes et signatures des chaînes hexadécimales, data une chaîne UTF-8.
"""

from collections.abc import Mapping

from .block import Block
from .errors import CodecError
from .serialization import is_uint64
from .transaction import Transaction

TRANSACTION_FIELDS = ("sender", "recipient", "amount", "data", "sequence", "hash", "signature")
BLOCK_FIELDS = ("index", "timestamp", "transactions", "prev_hash", "difficulty", "nonce", "hash")


def transaction_to_dict(transaction: Transaction) -> dict:
    """Dictionnaire JSON complet d'une transaction (hash et signature compris)."""
    if not isinstance(transaction, Transaction):
        raise CodecError(f"objet Transaction attendu, reçu {type(transaction).__name__}")
    return {field: getattr(transaction, field) for field in TRANSACTION_FIELDS}


def transaction_from_dict(data: object) -> Transaction:
    """Transaction reconstruite depuis un dictionnaire (forme vérifiée, règles non vérifiées)."""
    fields = _require_fields(data, TRANSACTION_FIELDS, "transaction")
    return Transaction(
        sender=_require_str(fields, "sender"),
        recipient=_require_str(fields, "recipient"),
        amount=_require_uint64(fields, "amount"),
        data=_require_str(fields, "data"),
        sequence=_require_uint64(fields, "sequence"),
        hash=_require_str(fields, "hash"),
        signature=_require_str(fields, "signature"),
    )


def block_to_dict(block: Block) -> dict:
    """Dictionnaire JSON complet d'un bloc, transactions incluses."""
    if not isinstance(block, Block):
        raise CodecError(f"objet Block attendu, reçu {type(block).__name__}")
    return {
        "index": block.index,
        "timestamp": block.timestamp,
        "transactions": [transaction_to_dict(transaction) for transaction in block.transactions],
        "prev_hash": block.prev_hash,
        "difficulty": block.difficulty,
        "nonce": block.nonce,
        "hash": block.hash,
    }


def block_from_dict(data: object) -> Block:
    """Bloc reconstruit depuis un dictionnaire (forme vérifiée, règles non vérifiées)."""
    fields = _require_fields(data, BLOCK_FIELDS, "bloc")
    raw_transactions = fields["transactions"]
    if not isinstance(raw_transactions, list):
        raise CodecError("bloc : « transactions » doit être une liste")
    transactions = []
    for position, raw in enumerate(raw_transactions):
        try:
            transactions.append(transaction_from_dict(raw))
        except CodecError as error:
            raise CodecError(f"bloc : transaction n°{position} : {error}") from None
    return Block(
        index=_require_uint64(fields, "index"),
        timestamp=_require_uint64(fields, "timestamp"),
        transactions=tuple(transactions),
        prev_hash=_require_str(fields, "prev_hash"),
        difficulty=_require_uint64(fields, "difficulty"),
        nonce=_require_uint64(fields, "nonce"),
        hash=_require_str(fields, "hash"),
    )


def blocks_to_list(blocks) -> list[dict]:
    return [block_to_dict(block) for block in blocks]


def blocks_from_list(data: object) -> tuple[Block, ...]:
    if not isinstance(data, list):
        raise CodecError("liste de blocs attendue")
    blocks = []
    for position, raw in enumerate(data):
        try:
            blocks.append(block_from_dict(raw))
        except CodecError as error:
            raise CodecError(f"bloc n°{position} de la liste : {error}") from None
    return tuple(blocks)


def _require_fields(data: object, expected: tuple[str, ...], kind: str) -> Mapping:
    if not isinstance(data, Mapping):
        raise CodecError(f"{kind} : dictionnaire attendu, reçu {type(data).__name__}")
    missing = [field for field in expected if field not in data]
    if missing:
        raise CodecError(f"{kind} : champ(s) manquant(s) : {', '.join(missing)}")
    unknown = [field for field in data if field not in expected]
    if unknown:
        raise CodecError(f"{kind} : champ(s) inconnu(s) : {', '.join(str(f) for f in unknown)}")
    return data


def _require_str(fields: Mapping, name: str) -> str:
    value = fields[name]
    if not isinstance(value, str):
        raise CodecError(f"« {name} » : chaîne attendue, reçu {type(value).__name__}")
    return value


def _require_uint64(fields: Mapping, name: str) -> int:
    value = fields[name]
    if not is_uint64(value):
        raise CodecError(f"« {name} » : entier entre 0 et 2^64-1 attendu, reçu {value!r}")
    return value
