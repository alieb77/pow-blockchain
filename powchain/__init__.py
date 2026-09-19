"""powchain : noyau d'une blockchain Proof of Work construite pas à pas.

Partie 1 : primitives cryptographiques, sérialisation canonique, transactions,
blocs, bloc Genesis et validation d'intégrité de la chaîne.
Partie 2 : preuve de travail (cible, difficulté, ajustement), minage, travail cumulé.
Partie 3 : clés Ed25519, adresses = clés publiques, transactions signées et vérifiées.
Partie 4 : état des comptes (soldes, séquences), coinbase et émission, mempool.
Partie 5 : réseau pair-à-pair : codec JSON, protocole, nœud (gossip,
synchronisation, règle du plus grand travail, borne d'horloge), transport
asyncio, réseau simulé, ligne de commande (python -m powchain).
Partie 6 : persistance sur disque (NodeStorage : blocs, mempool, carnet
d'adresses ; revalidation complète au chargement, réparation d'une fin de
fichier tronquée, écritures atomiques).
Partie 7 : wallet (Wallet : clés chiffrées par mot de passe via scrypt +
AES-256-GCM, somme de contrôle d'adresse façon EIP-55, sauvegarde par graine).
Partie 8 : miner vers son wallet (CLI « node --mine-label » : le nœud résout
une clé du wallet en adresse publique, sans mot de passe ni graine).
Partie 9 : ouverture au réseau (CLI « node --public » : écoute sur toutes les
interfaces ; portée des adresses, adresse propre apprise, plafond de
connexions entrantes, délai de hello).
Partie 10 : résilience (Node.tick : rappel des pairs avec délai croissant,
amorces --peers jamais oubliées, oubli des adresses mortes, bannissement
temporaire des hôtes fautifs).
Partie 11 : frais de transaction (champ fee signé, débité avec le montant,
reversé au mineur par la coinbase ; mempool servi par frais décroissant,
minimum relayé MIN_RELAY_FEE et éviction des moins payantes : anti-spam).
Partie 12 : API HTTP (ApiServer : serveur HTTP/1.1 minimal sur asyncio, JSON,
CORS ; lecture de la chaîne, des comptes, du mempool et des pairs ; soumission
de transactions signées ; index transaction/adresse dans Blockchain ; sert
aussi l'explorateur de blocs web à la racine pour un navigateur).
Partie 13 : limite de débit par pair (seau à jetons par connexion, MESSAGE_RATE
jetons/s plafonnés à MESSAGE_BURST ; un flot de messages même valides vaut une
déconnexion et un ban, comme une ligne illisible ; anti-spam avant l'ouverture
à un large public). Marque : monnaie FLOUS, ticker FLS (COIN_NAME / COIN_SYMBOL,
affichage) ; le format canonique hashé garde le préfixe interne « powchain/… ».

Ce module ré-exporte l'API publique pour permettre d'écrire simplement :

    from powchain import KeyPair, Blockchain, Mempool, create_signed_transaction, create_block, mine_block
"""

from .address import (
    ADDRESS_LENGTH,
    has_valid_checksum,
    is_valid_address,
    normalize_address,
    to_checksummed_address,
)
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
from .codec import block_from_dict, block_to_dict, transaction_from_dict, transaction_to_dict
from .crypto import HASH_HEX_LENGTH, is_valid_hash_hex, sha256_hex
from .errors import (
    CodecError,
    InvalidBlockError,
    InvalidChainError,
    InvalidTransactionError,
    MempoolError,
    MiningError,
    MiningLimitError,
    PowChainError,
    ProtocolError,
    SerializationError,
    StorageError,
    ValidationError,
    WalletError,
)
from .keys import (
    SIGNATURE_HEX_LENGTH,
    KeyPair,
    aead_decrypt,
    aead_encrypt,
    derive_symmetric_key,
    is_valid_public_key_hex,
    is_valid_signature_hex,
    verify_signature,
)
from .mempool import DEFAULT_MAX_SIZE, Mempool
from .mining import MiningResult, mine_block
from .api import API_PORT_OFFSET, API_VERSION, ApiServer
from .network import DIAL_TIMEOUT_SECONDS, HELLO_TIMEOUT_SECONDS, TICK_INTERVAL_SECONDS, NodeServer, local_ip_addresses
from .node import (
    BAN_SECONDS,
    DIAL_GRACE_SECONDS,
    MAX_DIAL_FAILURES,
    MAX_FUTURE_DRIFT_SECONDS,
    MAX_INBOUND,
    MAX_PEERS,
    MESSAGE_BURST,
    MESSAGE_RATE,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    AddressForgotten,
    AddressLearned,
    BlockAdded,
    ChainReorganized,
    Connect,
    Disconnect,
    Node,
    Peer,
    Send,
    TransactionAdded,
)
from .money import (
    COIN_NAME,
    COIN_SYMBOL,
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD,
    MAX_MONEY,
    MIN_RELAY_FEE,
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
from .protocol import (
    LOOPBACK,
    PRIVATE,
    PROTOCOL_VERSION,
    PUBLIC,
    Message,
    decode_message,
    encode_message,
    host_reaches,
    host_scope,
    message,
)
from .simulation import FakeClock, SimulatedNetwork
from .state import Account, State
from .storage import NodeStorage
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
from .wallet import DEFAULT_WALLET_PATH, Wallet, WalletEntry

__version__ = "0.13.0"

__all__ = [
    "ADDRESS_LENGTH",
    "ADJUSTMENT_DIVISOR",
    "COINBASE_ADDRESS",
    "COIN_NAME",
    "COIN_SYMBOL",
    "DEFAULT_WALLET_PATH",
    "Wallet",
    "WalletEntry",
    "WalletError",
    "aead_decrypt",
    "aead_encrypt",
    "derive_symmetric_key",
    "has_valid_checksum",
    "normalize_address",
    "to_checksummed_address",
    "AddressForgotten",
    "AddressLearned",
    "BlockAdded",
    "ChainReorganized",
    "NodeStorage",
    "StorageError",
    "TransactionAdded",
    "Connect",
    "CodecError",
    "Disconnect",
    "BAN_SECONDS",
    "DIAL_GRACE_SECONDS",
    "DIAL_TIMEOUT_SECONDS",
    "FakeClock",
    "HELLO_TIMEOUT_SECONDS",
    "LOOPBACK",
    "MAX_DIAL_FAILURES",
    "MAX_FUTURE_DRIFT_SECONDS",
    "MAX_INBOUND",
    "MAX_PEERS",
    "MESSAGE_BURST",
    "MESSAGE_RATE",
    "PRIVATE",
    "PUBLIC",
    "RECONNECT_BASE_DELAY",
    "RECONNECT_MAX_DELAY",
    "TICK_INTERVAL_SECONDS",
    "Message",
    "Node",
    "NodeServer",
    "ApiServer",
    "API_PORT_OFFSET",
    "API_VERSION",
    "PROTOCOL_VERSION",
    "Peer",
    "ProtocolError",
    "Send",
    "SimulatedNetwork",
    "block_from_dict",
    "block_to_dict",
    "decode_message",
    "encode_message",
    "host_reaches",
    "host_scope",
    "local_ip_addresses",
    "message",
    "transaction_from_dict",
    "transaction_to_dict",
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
    "MIN_RELAY_FEE",
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
