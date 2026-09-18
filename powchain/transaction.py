"""Transaction : structure, hash, signature, coinbase, création et validation.

Une Transaction est un enregistrement immuable (dataclass frozen). Deux
champs sont dérivés des autres et ne sont jamais fournis « à la main » :

* hash      : SHA-256 canonique de (sender, recipient, amount, data, sequence).
              C'est l'IDENTITÉ de la transaction : ce qu'elle dit.
* signature : Ed25519 des 32 octets du hash, par la clé privée dont sender
              est la clé publique. C'est l'AUTORISATION : qui l'a dit.

Le hash ne couvre pas la signature (Ed25519 étant déterministe, une même
transaction n'a de toute façon qu'une seule signature valide) ; la signature
couvre tout le hash, donc tout le contenu. L'étiquette de domaine
"powchain/tx/v2" incluse dans le hash fait qu'une signature n'est valable
que pour CE format de CETTE chaîne.

Le champ sequence (numéro de séquence) est le compteur anti-rejeu du compte
expéditeur : la n-ième transaction émise par un compte porte sequence = n-1.
Il est signé, et vérifié contre l'état des comptes (state.py, règle S1).

Coinbase (Partie 4)
-------------------
La seule façon de créer des pièces : une transaction dont sender vaut
COINBASE_ADDRESS ("0" * 64), une adresse RÉSERVÉE à laquelle aucune clé privée
ne correspond. Elle n'est pas signée ; ce qui l'autorise, c'est sa position
dans un bloc miné (règle B7 de block.py : unique, en première position,
amount = block_reward(hauteur), sequence = hauteur du bloc, ce qui rend
chaque coinbase unique). Le mempool refuse toute coinbase soumise par un
utilisateur, et l'état ne débite jamais COINBASE_ADDRESS.

Règles de validité (validate_transaction) :
  R1  sender et recipient sont des adresses valides (clé publique hex 64 car.)
  R2  amount est un entier avec 0 <= amount <= MAX_MONEY
  R3  data est une chaîne d'au plus MAX_DATA_BYTES octets UTF-8 (vide autorisée)
  R4  la transaction n'est pas « vide » : amount > 0 OU data non vide
      (exemption : une coinbase peut valoir 0 quand la récompense s'éteint)
  R5  sequence est un entier représentable en uint64
  R6  hash est un hexadécimal canonique ET égal au hash recalculé
  R7  transaction normale : signature de 128 caractères hexadécimaux qui
      vérifie le hash avec la clé publique sender
      coinbase : AUCUNE signature (signature == UNSIGNED)
"""

from dataclasses import dataclass, replace

from .address import ADDRESS_LENGTH, is_valid_address
from .crypto import hash_hex_to_bytes, is_valid_hash_hex, sha256_hex
from .errors import InvalidTransactionError, SerializationError
from .keys import SIGNATURE_HEX_LENGTH, KeyPair, is_valid_signature_hex, verify_signature
from .money import MAX_MONEY, block_reward, is_valid_amount
from .serialization import is_uint64, serialize_transaction_fields

MAX_DATA_BYTES = 1024
UNSIGNED = ""  # valeur du champ signature d'une transaction non signée (et de toute coinbase)
COINBASE_ADDRESS = "0" * ADDRESS_LENGTH  # adresse réservée : expéditeur des récompenses de minage


@dataclass(frozen=True, slots=True)
class Transaction:
    """Transfert de amount unités de sender vers recipient, avec data optionnelle.

    Ne pas construire directement en usage normal : passer par
    create_signed_transaction() (ou create_transaction() puis sign_transaction()),
    et par create_coinbase_transaction() pour la récompense d'un bloc.
    """

    sender: str
    recipient: str
    amount: int
    data: str
    sequence: int
    hash: str
    signature: str

    def calculate_hash(self) -> str:
        """Recalcule le hash à partir des champs réels (ignore hash et signature)."""
        return calculate_transaction_hash(
            self.sender, self.recipient, self.amount, self.data, self.sequence
        )

    def signing_message(self) -> bytes:
        """Les 32 octets que la clé de sender signe : le hash canonique recalculé."""
        return hash_hex_to_bytes(self.calculate_hash())

    @property
    def is_signed(self) -> bool:
        return self.signature != UNSIGNED

    @property
    def is_coinbase(self) -> bool:
        return self.sender == COINBASE_ADDRESS


def calculate_transaction_hash(
    sender: str, recipient: str, amount: int, data: str, sequence: int
) -> str:
    """SHA-256 de la sérialisation canonique : sender | recipient | amount | data | sequence."""
    return sha256_hex(serialize_transaction_fields(sender, recipient, amount, data, sequence))


def create_transaction(
    sender: str, recipient: str, amount: int, data: str = "", sequence: int = 0
) -> Transaction:
    """Transaction NON signée (signature vide) : valide seulement après sign_transaction()."""
    _validate_content_fields(sender, recipient, amount, data, sequence)
    return Transaction(
        sender=sender,
        recipient=recipient,
        amount=amount,
        data=data,
        sequence=sequence,
        hash=calculate_transaction_hash(sender, recipient, amount, data, sequence),
        signature=UNSIGNED,
    )


def create_coinbase_transaction(miner_address: str, height: int, data: str = "") -> Transaction:
    """Transaction de récompense du bloc de hauteur height (>= 1), payée à miner_address.

    sender = COINBASE_ADDRESS, amount = block_reward(height), sequence = height,
    signature = UNSIGNED. data est libre (message du mineur).
    """
    if not is_uint64(height) or height < 1:
        raise InvalidTransactionError(f"hauteur de bloc >= 1 attendue, reçu {height!r}")
    amount = block_reward(height)
    _validate_content_fields(COINBASE_ADDRESS, miner_address, amount, data, height)
    return Transaction(
        sender=COINBASE_ADDRESS,
        recipient=miner_address,
        amount=amount,
        data=data,
        sequence=height,
        hash=calculate_transaction_hash(COINBASE_ADDRESS, miner_address, amount, data, height),
        signature=UNSIGNED,
    )


def sign_transaction(transaction: Transaction, key_pair: KeyPair) -> Transaction:
    """Copie signée de transaction. key_pair doit être la clé de transaction.sender."""
    if not isinstance(transaction, Transaction):
        raise InvalidTransactionError(f"objet Transaction attendu, reçu {type(transaction).__name__}")
    if not isinstance(key_pair, KeyPair):
        raise InvalidTransactionError(f"KeyPair attendu, reçu {type(key_pair).__name__}")
    if transaction.is_coinbase:
        raise InvalidTransactionError("une coinbase ne se signe pas : c'est sa place dans un bloc miné qui l'autorise")
    _validate_content_fields(
        transaction.sender, transaction.recipient, transaction.amount, transaction.data, transaction.sequence
    )
    if key_pair.address != transaction.sender:
        raise InvalidTransactionError("la clé fournie n'est pas celle de l'expéditeur (sender)")
    return replace(
        transaction,
        hash=transaction.calculate_hash(),
        signature=key_pair.sign_hex(transaction.signing_message()),
    )


def create_signed_transaction(
    key_pair: KeyPair, recipient: str, amount: int, data: str = "", sequence: int = 0
) -> Transaction:
    """Construit et signe en une étape une transaction émise par key_pair."""
    if not isinstance(key_pair, KeyPair):
        raise InvalidTransactionError(f"KeyPair attendu, reçu {type(key_pair).__name__}")
    unsigned = create_transaction(key_pair.address, recipient, amount, data, sequence)
    return sign_transaction(unsigned, key_pair)


def verify_transaction_signature(transaction: object) -> bool:
    """Règle R7 seule, pour une transaction normale : signature de sender sur le hash recalculé."""
    if not isinstance(transaction, Transaction) or transaction.is_coinbase:
        return False
    try:
        message = transaction.signing_message()
    except SerializationError:
        return False
    return verify_signature(transaction.sender, transaction.signature, message)


def _validate_content_fields(
    sender: object, recipient: object, amount: object, data: object, sequence: object
) -> None:
    """Règles R1 à R5 : tout ce qui concerne le contenu, hors hash et signature."""
    if not is_valid_address(sender):
        raise InvalidTransactionError(
            f"sender invalide : {sender!r} (clé publique hexadécimale de {ADDRESS_LENGTH} caractères attendue)"
        )
    if not is_valid_address(recipient):
        raise InvalidTransactionError(
            f"recipient invalide : {recipient!r} (clé publique hexadécimale de {ADDRESS_LENGTH} caractères attendue)"
        )
    if not is_valid_amount(amount):
        raise InvalidTransactionError(
            f"amount invalide : {amount!r} (entier attendu entre 0 et {MAX_MONEY} unités)"
        )
    if not isinstance(data, str):
        raise InvalidTransactionError(f"data invalide : chaîne attendue, reçu {type(data).__name__}")
    try:
        data_size = len(data.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidTransactionError("data invalide : non encodable en UTF-8") from None
    if data_size > MAX_DATA_BYTES:
        raise InvalidTransactionError(f"data trop longue : {data_size} octets > {MAX_DATA_BYTES}")
    if amount == 0 and data == "" and sender != COINBASE_ADDRESS:
        raise InvalidTransactionError("transaction vide : amount = 0 et data vide")
    if not is_uint64(sequence):
        raise InvalidTransactionError(f"sequence invalide : {sequence!r} (entier >= 0 attendu)")


def validate_transaction(transaction: object) -> None:
    """Lève InvalidTransactionError si une des règles R1 à R7 est violée.

    Ne vérifie PAS l'état des comptes (séquence attendue, solde) : voir state.py.
    """
    if not isinstance(transaction, Transaction):
        raise InvalidTransactionError(
            f"objet Transaction attendu, reçu {type(transaction).__name__}"
        )
    _validate_content_fields(
        transaction.sender, transaction.recipient, transaction.amount, transaction.data, transaction.sequence
    )
    if not is_valid_hash_hex(transaction.hash):
        raise InvalidTransactionError(f"hash mal formé : {transaction.hash!r}")
    expected_hash = transaction.calculate_hash()
    if transaction.hash != expected_hash:
        raise InvalidTransactionError(
            f"hash incohérent : stocké {transaction.hash[:16]}..., recalculé {expected_hash[:16]}..."
        )
    if transaction.is_coinbase:
        if transaction.signature != UNSIGNED:
            raise InvalidTransactionError("coinbase : ne doit pas porter de signature")
        return
    if transaction.signature == UNSIGNED:
        raise InvalidTransactionError("transaction non signée")
    if not is_valid_signature_hex(transaction.signature):
        raise InvalidTransactionError(
            f"signature mal formée : {SIGNATURE_HEX_LENGTH} caractères hexadécimaux attendus"
        )
    if not verify_signature(transaction.sender, transaction.signature, hash_hex_to_bytes(expected_hash)):
        raise InvalidTransactionError(
            "signature invalide : elle ne provient pas de la clé de sender ou ne couvre pas ce contenu"
        )


def is_valid_transaction(transaction: object) -> bool:
    """Version booléenne de validate_transaction()."""
    try:
        validate_transaction(transaction)
    except InvalidTransactionError:
        return False
    return True
