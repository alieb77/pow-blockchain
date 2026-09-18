"""Mempool : file d'attente des transactions pas encore incluses dans un bloc.

Quand un utilisateur émet une transaction signée, elle n'est pas dans la
chaîne : elle attend qu'un mineur la place dans un bloc. Le mempool est
cette salle d'attente. Il ne modifie jamais la chaîne ni l'état ; il ne fait
que trier ce qui mérite d'attendre.

Règles d'admission (add) :
    M1  transaction structurellement valide (R1-R7) et pas une coinbase
        (les coinbases sont créées par le mineur, jamais soumises)
    M2  pas déjà en attente (même hash)
    M3  valide dans l'état PROJETÉ = état courant + transactions en attente,
        appliquées dans l'ordre d'arrivée. Ainsi alice peut soumettre sa
        séquence 0 puis sa séquence 1 sans attendre un bloc, mais ne peut pas
        engager plus que son solde projeté ni sauter un numéro.
    M4  capacité max_size non dépassée

Ordre de service : arrivée (FIFO). Les vraies chaînes trient par frais
offerts au mineur ; il n'y a pas encore de frais ici.

Cycle de vie côté mineur :
    txs = mempool.select(chain.state)                      # ce qui passe, dans l'ordre
    block = mine_block(create_block(chain.last_block, txs, miner)).block
    chain.add_block(block)
    mempool.remove_confirmed(block, chain.state)           # purge et re-validation
"""

from .block import MAX_TRANSACTIONS_PER_BLOCK, Block
from .errors import InvalidTransactionError, MempoolError
from .state import State
from .transaction import Transaction, validate_transaction

DEFAULT_MAX_SIZE = 10_000


class Mempool:
    """Transactions en attente, dans l'ordre d'arrivée, validées contre un État projeté."""

    __slots__ = ("_pending", "_max_size")

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < 1:
            raise ValueError(f"max_size >= 1 attendu, reçu {max_size!r}")
        self._pending: dict[str, Transaction] = {}
        self._max_size = max_size

    def __len__(self) -> int:
        return len(self._pending)

    def __contains__(self, item: object) -> bool:
        key = item.hash if isinstance(item, Transaction) else item
        return key in self._pending

    @property
    def transactions(self) -> tuple[Transaction, ...]:
        """Transactions en attente, dans l'ordre d'arrivée."""
        return tuple(self._pending.values())

    def add(self, transaction: Transaction, state: State) -> None:
        """Admet transaction (règles M1-M4) ou lève InvalidTransactionError / MempoolError."""
        validate_transaction(transaction)
        if transaction.is_coinbase:
            raise MempoolError("les coinbases sont créées par le mineur, elles ne se soumettent pas")
        if transaction.hash in self._pending:
            raise MempoolError(f"transaction déjà en attente : {transaction.hash[:16]}...")
        if len(self._pending) >= self._max_size:
            raise MempoolError(f"mempool plein ({self._max_size} transactions)")
        self.projected_state(state).apply_transaction(transaction)
        self._pending[transaction.hash] = transaction

    def projected_state(self, state: State) -> State:
        """État courant + transactions en attente applicables, dans l'ordre d'arrivée."""
        projected = _require_state(state)
        for transaction in self._pending.values():
            try:
                projected = projected.apply_transaction(transaction)
            except InvalidTransactionError:
                continue  # devenue inapplicable : sera purgée par remove_confirmed()
        return projected

    def select(
        self, state: State, max_transactions: int = MAX_TRANSACTIONS_PER_BLOCK - 1
    ) -> tuple[Transaction, ...]:
        """Transactions à inclure dans le prochain bloc : applicables sur state, dans l'ordre.

        max_transactions laisse par défaut une place pour la coinbase.
        """
        projected = _require_state(state)
        selected: list[Transaction] = []
        for transaction in self._pending.values():
            if len(selected) >= max_transactions:
                break
            try:
                projected = projected.apply_transaction(transaction)
            except InvalidTransactionError:
                continue
            selected.append(transaction)
        return tuple(selected)

    def remove_confirmed(self, block: Block, state: State) -> tuple[Transaction, ...]:
        """Retire les transactions incluses dans block, puis purge celles devenues
        inapplicables sur state (séquence consommée, solde disparu).

        Retourne les transactions purgées (non incluses mais abandonnées).
        """
        if not isinstance(block, Block):
            raise TypeError(f"objet Block attendu, reçu {type(block).__name__}")
        confirmed = {transaction.hash for transaction in block.transactions}
        projected = _require_state(state)
        kept: dict[str, Transaction] = {}
        dropped: list[Transaction] = []
        for tx_hash, transaction in self._pending.items():
            if tx_hash in confirmed:
                continue
            try:
                projected = projected.apply_transaction(transaction)
            except InvalidTransactionError:
                dropped.append(transaction)
                continue
            kept[tx_hash] = transaction
        self._pending = kept
        return tuple(dropped)


def _require_state(state: object) -> State:
    if not isinstance(state, State):
        raise TypeError(f"objet State attendu, reçu {type(state).__name__}")
    return state
