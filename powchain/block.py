"""Bloc : structure, hash, bloc Genesis, création et validation (avec preuve de travail).

Un Block est un enregistrement immuable. Son hash est le SHA-256 de son
« en-tête » canonique (voir serialization.py) :

    index | timestamp | transactions_hash | prev_hash | difficulty | nonce

où transactions_hash = SHA-256 de la liste ORDONNÉE des hashes des
transactions (calculate_transactions_hash). Conséquences :

* modifier n'importe quel champ d'une transaction change son hash, donc
  transactions_hash, donc le hash du bloc ;
* changer l'ordre des transactions change aussi le hash du bloc ;
* le minage ne re-hashe que ce petit en-tête à chaque essai de nonce, jamais
  les transactions complètes ;
* transactions_hash pourra être remplacé par une racine de Merkle sans
  toucher à la structure Block.

Cycle de vie d'un bloc depuis la Partie 2 :

    create_block()  -> candidat : nonce = 0, difficulté attendue, hash calculé
                       mais (presque sûrement) supérieur à la cible
    mine_block()    -> bloc miné : nonce trouvé, hash <= cible   (mining.py)
    add_block()     -> accepté seulement si validate_block() passe (chain.py)

Conventions du bloc Genesis (bloc n°0, identique sur toutes les machines) :

    index        = 0
    timestamp    = GENESIS_TIMESTAMP, constante fixée à 2026-01-01T00:00:00Z.
                   JAMAIS l'heure système : sinon chaque machine aurait un
                   Genesis différent et aucune chaîne ne serait comparable.
    transactions = () aucune. Les premières pièces seront créées par les
                   récompenses de minage.
    prev_hash    = "0" * 64 : il n'y a pas de bloc précédent.
    difficulty   = INITIAL_DIFFICULTY.
    nonce        = GENESIS_NONCE : le Genesis obéit à la même règle de preuve
                   de travail que les autres blocs. Son nonce a été miné une
                   fois pour toutes puis figé dans le code, exactement comme
                   le nonce 2083236893 du Genesis de Bitcoin.
    hash         = calculé exactement comme pour tout autre bloc.

Règles de validité d'un bloc (validate_block) :
    B1  types et bornes des champs de l'en-tête (index, timestamp, difficulty,
        nonce en uint64 ; prev_hash et hash en hexadécimal canonique)
    B2  transactions : tuple, chacune valide, aucun hash en double
    B3  chaînage : index = précédent + 1, prev_hash = hash du précédent,
        timestamp strictement supérieur à celui du précédent
    B4  difficulty = difficulté attendue par la règle d'ajustement
        (proof_of_work.expected_difficulty) ; GENESIS_DIFFICULTY pour le bloc 0
    B5  hash stocké = hash recalculé
    B6  preuve de travail : hash <= cible de la difficulté du bloc
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass

from .crypto import HASH_HEX_LENGTH, is_valid_hash_hex, sha256_hex
from .errors import InvalidBlockError, InvalidTransactionError, SerializationError
from .proof_of_work import (
    INITIAL_DIFFICULTY,
    expected_difficulty,
    hash_meets_target,
    is_valid_difficulty,
    target_from_difficulty,
)
from .serialization import is_uint64, serialize_block_header, serialize_transaction_hash_list
from .transaction import Transaction, validate_transaction

GENESIS_INDEX = 0
GENESIS_TIMESTAMP = 1_767_225_600  # 2026-01-01T00:00:00Z, secondes Unix
GENESIS_PREV_HASH = "0" * HASH_HEX_LENGTH
GENESIS_DIFFICULTY = INITIAL_DIFFICULTY
GENESIS_NONCE = 5237  # solution de la preuve de travail du Genesis, minée une fois puis figée
GENESIS_TRANSACTIONS: tuple[Transaction, ...] = ()

# Nonce de départ de tout nouveau candidat ; mine_block() le fait varier.
INITIAL_NONCE = 0


@dataclass(frozen=True, slots=True)
class Block:
    """Bloc de la chaîne. Construire via create_genesis_block() ou create_block()."""

    index: int
    timestamp: int
    transactions: tuple[Transaction, ...]
    prev_hash: str
    difficulty: int
    nonce: int
    hash: str

    def calculate_transactions_hash(self) -> str:
        """Empreinte de la liste ordonnée des transactions du bloc."""
        return calculate_transactions_hash(self.transactions)

    def calculate_hash(self) -> str:
        """Recalcule le hash de l'en-tête à partir des champs réels (ignore self.hash)."""
        return calculate_block_hash(
            self.index,
            self.timestamp,
            self.calculate_transactions_hash(),
            self.prev_hash,
            self.difficulty,
            self.nonce,
        )

    @property
    def target(self) -> int:
        """Cible numérique que le hash de ce bloc doit respecter."""
        return target_from_difficulty(self.difficulty)

    def has_valid_proof_of_work(self) -> bool:
        """Vrai si le hash stocké respecte la cible (ne vérifie pas que le hash est juste)."""
        return hash_meets_target(self.hash, self.difficulty) if is_valid_difficulty(self.difficulty) else False


def calculate_transactions_hash(transactions: Sequence[Transaction]) -> str:
    """SHA-256 de la liste ordonnée des hashes des transactions."""
    for position, transaction in enumerate(transactions):
        if not isinstance(transaction, Transaction):
            raise SerializationError(f"transaction n°{position} : objet Transaction attendu")
    return sha256_hex(serialize_transaction_hash_list([tx.hash for tx in transactions]))


def calculate_block_hash(
    index: int, timestamp: int, transactions_hash: str, prev_hash: str, difficulty: int, nonce: int
) -> str:
    """SHA-256 de l'en-tête canonique : index | timestamp | transactions_hash | prev_hash | difficulty | nonce."""
    return sha256_hex(
        serialize_block_header(index, timestamp, transactions_hash, prev_hash, difficulty, nonce)
    )


def create_genesis_block() -> Block:
    """Construit LE bloc Genesis, identique à chaque appel et sur toute machine."""
    return _build_block(
        GENESIS_INDEX,
        GENESIS_TIMESTAMP,
        GENESIS_TRANSACTIONS,
        GENESIS_PREV_HASH,
        GENESIS_DIFFICULTY,
        GENESIS_NONCE,
    )


def create_block(
    prev_block: Block, transactions: Sequence[Transaction], timestamp: int | None = None
) -> Block:
    """Construit un CANDIDAT chaîné à prev_block : nonce = INITIAL_NONCE, difficulté attendue.

    Le hash du candidat est calculé mais ne respecte presque sûrement pas la
    cible : il faut passer par mining.mine_block() avant Blockchain.add_block().

    Timestamp par défaut : l'heure système, forcée à être strictement
    supérieure à celle du bloc précédent (règle B3).
    """
    _require_well_formed_block(prev_block, "prev_block")
    transaction_tuple = tuple(transactions)
    _validate_transactions(transaction_tuple)
    if timestamp is None:
        timestamp = max(int(time.time()), prev_block.timestamp + 1)
    if not is_uint64(timestamp) or timestamp <= prev_block.timestamp:
        raise InvalidBlockError(
            f"timestamp {timestamp!r} : entier strictement supérieur à {prev_block.timestamp} attendu"
        )
    difficulty = expected_difficulty(prev_block.difficulty, prev_block.timestamp, timestamp)
    return _build_block(
        prev_block.index + 1,
        timestamp,
        transaction_tuple,
        prev_block.hash,
        difficulty,
        INITIAL_NONCE,
    )


def _build_block(
    index: int,
    timestamp: int,
    transactions: tuple[Transaction, ...],
    prev_hash: str,
    difficulty: int,
    nonce: int,
) -> Block:
    """Assemble un bloc et calcule son hash (unique point de construction)."""
    transactions_hash = calculate_transactions_hash(transactions)
    return Block(
        index=index,
        timestamp=timestamp,
        transactions=transactions,
        prev_hash=prev_hash,
        difficulty=difficulty,
        nonce=nonce,
        hash=calculate_block_hash(index, timestamp, transactions_hash, prev_hash, difficulty, nonce),
    )


def _validate_transactions(transactions: object) -> None:
    """Règle B2 : chaque transaction est valide et aucun hash n'apparaît deux fois."""
    if not isinstance(transactions, tuple):
        raise InvalidBlockError(
            f"transactions : tuple attendu, reçu {type(transactions).__name__}"
        )
    seen_hashes: set[str] = set()
    for position, transaction in enumerate(transactions):
        try:
            validate_transaction(transaction)
        except InvalidTransactionError as error:
            raise InvalidBlockError(f"transaction n°{position} invalide : {error}") from None
        if transaction.hash in seen_hashes:
            raise InvalidBlockError(
                f"transaction n°{position} dupliquée : {transaction.hash[:16]}..."
            )
        seen_hashes.add(transaction.hash)


def validate_block(block: object, prev_block: Block | None = None) -> None:
    """Lève InvalidBlockError si une des règles B1 à B6 est violée.

    prev_block = None signifie « ce bloc doit être un bloc n°0 » (conventions
    de chaînage du Genesis) ; la conformité exacte au Genesis canonique est
    vérifiée au niveau de la chaîne (chain.py).
    """
    if not isinstance(block, Block):
        raise InvalidBlockError(f"objet Block attendu, reçu {type(block).__name__}")
    _validate_header_fields(block)
    _validate_transactions(block.transactions)
    _validate_link(block, prev_block)
    _validate_difficulty(block, prev_block)
    expected_hash = block.calculate_hash()
    if block.hash != expected_hash:
        raise InvalidBlockError(
            f"bloc n°{block.index} : hash incohérent "
            f"(stocké {block.hash[:16]}..., recalculé {expected_hash[:16]}...)"
        )
    _validate_proof_of_work(block)


def _require_well_formed_block(candidate: object, role: str) -> None:
    """Type et champs d'en-tête d'un bloc utilisé comme référence (prev_block)."""
    if not isinstance(candidate, Block):
        raise InvalidBlockError(f"{role} : objet Block attendu, reçu {type(candidate).__name__}")
    try:
        _validate_header_fields(candidate)
    except InvalidBlockError as error:
        raise InvalidBlockError(f"{role} : {error}") from None


def _validate_header_fields(block: Block) -> None:
    """Règle B1 : types et bornes de chaque champ de l'en-tête (rend la sérialisation sûre)."""
    if not is_uint64(block.index):
        raise InvalidBlockError(f"index invalide : {block.index!r}")
    if not is_uint64(block.timestamp):
        raise InvalidBlockError(f"timestamp invalide : {block.timestamp!r}")
    if not is_valid_difficulty(block.difficulty):
        raise InvalidBlockError(f"difficulté invalide : {block.difficulty!r}")
    if not is_uint64(block.nonce):
        raise InvalidBlockError(f"nonce invalide : {block.nonce!r}")
    if not is_valid_hash_hex(block.prev_hash):
        raise InvalidBlockError(f"prev_hash mal formé : {block.prev_hash!r}")
    if not is_valid_hash_hex(block.hash):
        raise InvalidBlockError(f"hash mal formé : {block.hash!r}")


def _validate_link(block: Block, prev_block: object) -> None:
    """Règle B3 : cohérence de l'index, du prev_hash et du timestamp avec le bloc précédent."""
    if prev_block is None:
        if block.index != GENESIS_INDEX:
            raise InvalidBlockError(f"bloc n°{block.index} : bloc précédent manquant")
        if block.prev_hash != GENESIS_PREV_HASH:
            raise InvalidBlockError(f"bloc n°0 : prev_hash doit valoir {GENESIS_PREV_HASH}")
        return
    _require_well_formed_block(prev_block, "prev_block")
    if block.index != prev_block.index + 1:
        raise InvalidBlockError(
            f"index incohérent : bloc n°{block.index} après bloc n°{prev_block.index}"
        )
    if block.prev_hash != prev_block.hash:
        raise InvalidBlockError(
            f"bloc n°{block.index} : prev_hash ne correspond pas au hash du bloc n°{prev_block.index}"
        )
    if block.timestamp <= prev_block.timestamp:
        raise InvalidBlockError(
            f"bloc n°{block.index} : timestamp {block.timestamp} doit être strictement "
            f"supérieur à celui du bloc n°{prev_block.index} ({prev_block.timestamp})"
        )


def _validate_difficulty(block: Block, prev_block: Block | None) -> None:
    """Règle B4 : la difficulté portée par le bloc est celle qu'impose la règle d'ajustement."""
    if prev_block is None:
        expected = GENESIS_DIFFICULTY
    else:
        expected = expected_difficulty(prev_block.difficulty, prev_block.timestamp, block.timestamp)
    if block.difficulty != expected:
        raise InvalidBlockError(
            f"bloc n°{block.index} : difficulté {block.difficulty} au lieu de {expected} "
            f"attendue par la règle d'ajustement"
        )


def _validate_proof_of_work(block: Block) -> None:
    """Règle B6 : le hash, lu comme un entier, est inférieur ou égal à la cible."""
    if not hash_meets_target(block.hash, block.difficulty):
        raise InvalidBlockError(
            f"bloc n°{block.index} : preuve de travail invalide "
            f"(hash supérieur à la cible de la difficulté {block.difficulty})"
        )


def is_valid_block(block: object, prev_block: Block | None = None) -> bool:
    """Version booléenne de validate_block()."""
    try:
        validate_block(block, prev_block)
    except InvalidBlockError:
        return False
    return True
