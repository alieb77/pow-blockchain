"""État des comptes : soldes et numéros de séquence dérivés de la chaîne.

Une blockchain ne stocke pas les soldes : elle stocke l'HISTORIQUE des
transactions. Le solde d'un compte est ce qu'on obtient en rejouant toutes
les transactions depuis le Genesis. Ce module fait exactement cela.

Modèle de comptes (comme Ethereum) plutôt que modèle UTXO (Bitcoin) :
chaque adresse a un solde et un compteur next_sequence. C'est le modèle
naturel pour nos transactions « sender paie amount à recipient » et pour les
futurs packs de jeu (un pack sera un enregistrement attaché à un compte).
Le modèle UTXO, où chaque pièce est une « sortie » consommée une seule fois,
est plus favorable à la parallélisation et à la confidentialité mais bien
plus complexe à exposer.

Règles d'état (appliquées transaction par transaction, dans l'ordre du bloc) :
    S1  sequence == next_sequence du compte expéditeur. Empêche le rejeu (une
        transaction confirmée ne peut pas l'être deux fois) et impose l'ordre
        d'émission d'un même compte.
    S2  solde du compte expéditeur >= amount. Interdit de dépenser ce qu'on
        n'a pas ; combinée à S1, interdit la double dépense.
    S3  aucun solde ne dépasse MAX_MONEY (garde-fou contre tout débordement).
    Coinbase : crédite simplement recipient, sans débiter personne : c'est
    la création monétaire. Sa légitimité (montant, unicité, position) est
    vérifiée par la règle B7 de block.py, pas ici.

State est IMMUABLE : apply_transaction() et apply_block() renvoient un nouvel
État et laissent l'ancien intact. Un bloc refusé ne peut donc jamais laisser
l'état à moitié modifié, et le mempool peut projeter librement.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .block import Block
from .errors import InvalidBlockError, InvalidTransactionError
from .money import MAX_MONEY, format_units, is_valid_amount
from .transaction import Transaction


@dataclass(frozen=True, slots=True)
class Account:
    """Solde (en unités) et prochain numéro de séquence attendu d'une adresse."""

    balance: int = 0
    next_sequence: int = 0


EMPTY_ACCOUNT = Account()


class State:
    """Photographie immuable des comptes après une suite de transactions."""

    __slots__ = ("_accounts",)

    def __init__(self, accounts: Mapping[str, Account] | None = None) -> None:
        self._accounts: dict[str, Account] = dict(accounts) if accounts else {}

    def account(self, address: str) -> Account:
        """Compte de l'adresse ; un compte inconnu vaut (0, 0)."""
        return self._accounts.get(address, EMPTY_ACCOUNT)

    def balance_of(self, address: str) -> int:
        return self.account(address).balance

    def next_sequence_of(self, address: str) -> int:
        return self.account(address).next_sequence

    @property
    def accounts(self) -> Mapping[str, Account]:
        """Vue en lecture seule de tous les comptes connus."""
        return MappingProxyType(self._accounts)

    @property
    def total_supply(self) -> int:
        """Somme de tous les soldes = total des récompenses de minage émises."""
        return sum(account.balance for account in self._accounts.values())

    def apply_transaction(self, transaction: Transaction) -> "State":
        """Nouvel État après transaction, ou InvalidTransactionError (S1, S2, S3).

        Suppose la transaction structurellement valide (R1-R7, voir
        validate_transaction) : seules les règles d'état sont vérifiées ici.
        """
        if not isinstance(transaction, Transaction):
            raise InvalidTransactionError(
                f"objet Transaction attendu, reçu {type(transaction).__name__}"
            )
        if not is_valid_amount(transaction.amount):
            raise InvalidTransactionError(f"amount invalide : {transaction.amount!r}")
        accounts = dict(self._accounts)
        if not transaction.is_coinbase:
            sender = self.account(transaction.sender)
            if transaction.sequence != sender.next_sequence:
                raise InvalidTransactionError(
                    f"sequence {transaction.sequence} : le compte {transaction.sender[:12]}... "
                    f"attend la sequence {sender.next_sequence}"
                )
            if sender.balance < transaction.amount:
                raise InvalidTransactionError(
                    f"solde insuffisant : {format_units(sender.balance)} disponible, "
                    f"{format_units(transaction.amount)} demandé"
                )
            accounts[transaction.sender] = Account(
                balance=sender.balance - transaction.amount,
                next_sequence=sender.next_sequence + 1,
            )
        recipient = accounts.get(transaction.recipient, EMPTY_ACCOUNT)
        new_balance = recipient.balance + transaction.amount
        if new_balance > MAX_MONEY:
            raise InvalidTransactionError(
                f"solde de {transaction.recipient[:12]}... dépasserait le plafond MAX_MONEY"
            )
        accounts[transaction.recipient] = Account(new_balance, recipient.next_sequence)
        return State(accounts)

    def apply_block(self, block: Block) -> "State":
        """Nouvel État après toutes les transactions du bloc, dans l'ordre, ou InvalidBlockError."""
        if not isinstance(block, Block):
            raise InvalidBlockError(f"objet Block attendu, reçu {type(block).__name__}")
        state = self
        for position, transaction in enumerate(block.transactions):
            try:
                state = state.apply_transaction(transaction)
            except InvalidTransactionError as error:
                raise InvalidBlockError(
                    f"bloc n°{block.index} : transaction n°{position} refusée par l'état : {error}"
                ) from None
        return state

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, State):
            return NotImplemented
        return self._accounts == other._accounts

    def __repr__(self) -> str:
        return f"State({len(self._accounts)} comptes, {format_units(self.total_supply)} en circulation)"
