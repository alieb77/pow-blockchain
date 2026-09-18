"""powchain : noyau d'une blockchain Proof of Work construite pas à pas.

Partie 1 : primitives cryptographiques, sérialisation canonique, transactions,
blocs, bloc Genesis et validation d'intégrité de la chaîne.
Partie 2 : preuve de travail (cible, difficulté, ajustement), minage, travail cumulé.
Partie 3 : clés Ed25519, adresses = clés publiques, transactions signées et vérifiées.
Partie 4 : état des comptes (soldes, séquences), coinbase et émission, mempool.

Ce module ré-exporte l'API publique pour permettre d'écrire simplement :

    from powchain import KeyPair, Blockchain, Mempool, create_signed_transaction, create_block, mine_block
"""

from .address import ADDRESS_LENGTH, is_valid_address
from .block import (
    GENESIS_DIFFICULTY,
    GENESIS_INDEX,
    GENESIS_NONCE,
    GENESIS_PREV_HASH,
    GENESIS_TIMESTAMP,
    INITIAL_NONCE,
    MAX_TRANSACTIONS_PER_BLOCK,
    Block,
    calculate_block_hash,
    calculate_transactions_hash,
    create_block,
    create_genesis_block,
    is_valid_block,
    validate_block,
)
from .chain import Blockchain, chain_work, compute_state, is_valid_chain, validate_chain
from .crypto import HASH_HEX_LENGTH, is_valid_hash_hex, sha256_hex
from .errors import (
    InvalidBlockError,
    InvalidChainError,
    InvalidTransactionError,
    MempoolError,
    MiningError,
    MiningLimitError,
    PowChainError,
    SerializationError,
    ValidationError,
)
from .keys import (
    SIGNATURE_HEX_LENGTH,
    KeyPair,
    is_valid_public_key_hex,
    is_valid_signature_hex,
    verify_signature,
)
from .mempool import DEFAULT_MAX_SIZE, Mempool
from .mining import MiningResult, mine_block
from .money import (
    COIN_SYMBOL,
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD,
    MAX_MONEY,
    UNITS_PER_COIN,
    block_reward,
    format_units,
    is_valid_amount,
    parse_coin_amount,
)
from .proof_of_work import (
    ADJUSTMENT_DIVISOR,
    INITIAL_DIFFICULTY,
    MAX_DIFFICULTY,
    MAX_TARGET,
    MIN_DIFFICULTY,
    TARGET_BLOCK_TIME,
    block_work,
    expected_difficulty,
    format_target,
    hash_meets_target,
    is_valid_difficulty,
    target_from_difficulty,
)
from .state import Account, State
from .transaction import (
    COINBASE_ADDRESS,
    MAX_DATA_BYTES,
    UNSIGNED,
    Transaction,
    calculate_transaction_hash,
    create_coinbase_transaction,
    create_signed_transaction,
    create_transaction,
    is_valid_transaction,
    sign_transaction,
    validate_transaction,
    verify_transaction_signature,
)

__version__ = "0.4.0"

__all__ = [
    "ADDRESS_LENGTH",
    "ADJUSTMENT_DIVISOR",
    "COINBASE_ADDRESS",
    "COIN_SYMBOL",
    "DEFAULT_MAX_SIZE",
    "GENESIS_DIFFICULTY",
    "GENESIS_INDEX",
    "GENESIS_NONCE",
    "GENESIS_PREV_HASH",
    "GENESIS_TIMESTAMP",
    "HALVING_INTERVAL",
    "HASH_HEX_LENGTH",
    "INITIAL_BLOCK_REWARD",
    "INITIAL_DIFFICULTY",
    "INITIAL_NONCE",
    "MAX_DATA_BYTES",
    "MAX_DIFFICULTY",
    "MAX_MONEY",
    "MAX_TARGET",
    "MAX_TRANSACTIONS_PER_BLOCK",
    "MIN_DIFFICULTY",
    "SIGNATURE_HEX_LENGTH",
    "TARGET_BLOCK_TIME",
    "UNITS_PER_COIN",
    "UNSIGNED",
    "Account",
    "Block",
    "Blockchain",
    "InvalidBlockError",
    "InvalidChainError",
    "InvalidTransactionError",
    "KeyPair",
    "Mempool",
    "MempoolError",
    "MiningError",
    "MiningLimitError",
    "MiningResult",
    "PowChainError",
    "SerializationError",
    "State",
    "Transaction",
    "ValidationError",
    "block_reward",
    "block_work",
    "calculate_block_hash",
    "calculate_transaction_hash",
    "calculate_transactions_hash",
    "chain_work",
    "compute_state",
    "create_block",
    "create_coinbase_transaction",
    "create_genesis_block",
    "create_signed_transaction",
    "create_transaction",
    "expected_difficulty",
    "format_target",
    "format_units",
    "hash_meets_target",
    "is_valid_address",
    "is_valid_amount",
    "is_valid_block",
    "is_valid_chain",
    "is_valid_difficulty",
    "is_valid_hash_hex",
    "is_valid_public_key_hex",
    "is_valid_signature_hex",
    "is_valid_transaction",
    "mine_block",
    "parse_coin_amount",
    "sha256_hex",
    "sign_transaction",
    "target_from_difficulty",
    "validate_block",
    "validate_chain",
    "validate_transaction",
    "verify_signature",
    "verify_transaction_signature",
]
