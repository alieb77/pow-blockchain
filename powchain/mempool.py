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
    M4  capacité : quand le mempool est plein (max_size), une transaction
        n'entre que si son frais dépasse le plus bas frais en attente ; la
        transaction la moins payante est alors évincée, avec celles du même
        expéditeur qui la suivent (elles ne pouvaient plus s'appliquer sans
        elle). Payer plus que les autres est donc le seul moyen d'entrer dans
        un mempool saturé : le spam a un coût qui monte avec la pression.
    M5  frais >= min_fee (Partie 11, politique de relais anti-spam) : une
        transaction qui paie moins n'est ni gardée ni relayée. La CHAÎNE, elle,
        accepte tout frais >= 0 : un mineur reste libre d'inclure ce qu'il
        veut dans SES blocs, il en paie le coût en preuve de travail.

Ordre de service (select) : par frais décroissant, à frais égal par ordre
d'arrivée, en respectant pour chaque expéditeur l'ordre de ses séquences (sa
transaction n°1 ne peut pas passer avant sa n°0). Une transaction qui ne
s'applique pas (périmée, solde insuffisant) est sautée ; les suivantes du même
expéditeur sont tout de même essayées (une séquence périmée n'empêche pas la
suivante d'être la bonne). Une transaction qui dépend d'un crédit encore en
attente (bob dépense ce qu'alice va lui envoyer) peut être servie un bloc plus
tard que dans l'ordre d'arrivée : c'est le prix de la priorité aux frais.

Cycle de vie côté mineur :
    txs = mempool.select(chain.state)                      # ce qui passe, frais d'abord
    block = mine_block(create_block(chain.last_block, txs, miner)).block
    chain.add_block(block)
    mempool.remove_confirmed(block, chain.state)           # purge et re-validation

Réorganisation (Partie 5) : quand un nœud abandonne sa branche pour une
chaîne plus lourde, les transactions des blocs abandonnés redeviennent
« en attente » et tout le mempool est re-validé contre le nouvel état :
    mempool.resync(new_state, extra=transactions_des_blocs_abandonnés)
"""

import heapq
from collections import deque

from .block import MAX_TRANSACTIONS_PER_BLOCK, Block
from .errors import InvalidTransactionError, MempoolError
from .money import MIN_RELAY_FEE, format_units, is_valid_amount
from .state import State
from .transaction import Transaction, validate_transaction

DEFAULT_MAX_SIZE = 10_000


class Mempool:
    """Transactions en attente, servies par frais décroissant, validées contre un État projeté."""

    __slots__ = ("_pending", "_max_size", "_min_fee")

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE, min_fee: int = MIN_RELAY_FEE) -> None:
        if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < 1:
            raise ValueError(f"max_size >= 1 attendu, reçu {max_size!r}")
        if not is_valid_amount(min_fee):
            raise ValueError(f"min_fee : entier entre 0 et MAX_MONEY attendu, reçu {min_fee!r}")
        self._pending: dict[str, Transaction] = {}
        self._max_size = max_size
        self._min_fee = min_fee

    def __len__(self) -> int:
        return len(self._pending)

    def __contains__(self, item: object) -> bool:
        key = item.hash if isinstance(item, Transaction) else item
        return key in self._pending

    @property
    def min_fee(self) -> int:
        """Frais minimal accepté (règle M5), en unités."""
        return self._min_fee

    @property
    def transactions(self) -> tuple[Transaction, ...]:
        """Transactions en attente, dans l'ordre d'arrivée."""
        return tuple(self._pending.values())

    def add(self, transaction: Transaction, state: State) -> tuple[Transaction, ...]:
        """Admet transaction (règles M1-M5) ou lève InvalidTransactionError / MempoolError.

        Retourne les transactions évincées pour lui faire place (règle M4),
        généralement aucune.
        """
        validate_transaction(transaction)
        if transaction.is_coinbase:
            raise MempoolError("les coinbases sont créées par le mineur, elles ne se soumettent pas")
        if transaction.hash in self._pending:
            raise MempoolError(f"transaction déjà en attente : {transaction.hash[:16]}...")
        if transaction.fee < self._min_fee:
            raise MempoolError(
                f"frais insuffisants : {format_units(transaction.fee)} "
                f"< minimum relayé {format_units(self._min_fee)}"
            )
        to_evict: tuple[Transaction, ...] = ()
        if len(self._pending) >= self._max_size:
            cheapest = self._cheapest()
            if transaction.fee <= cheapest.fee:
                raise MempoolError(
                    f"mempool plein ({self._max_size} transactions) et frais "
                    f"{format_units(transaction.fee)} pas supérieurs au plus bas en attente "
                    f"({format_units(cheapest.fee)})"
                )
            to_evict = self._dependents(cheapest)
        skipped = {evicted.hash for evicted in to_evict}
        self._project(state, skipped).apply_transaction(transaction)
        for evicted in to_evict:
            del self._pending[evicted.hash]
        self._pending[transaction.hash] = transaction
        return to_evict

    def projected_state(self, state: State) -> State:
        """État courant + transactions en attente applicables, dans l'ordre d'arrivée."""
        return self._project(state, frozenset())

    def select(
        self, state: State, max_transactions: int = MAX_TRANSACTIONS_PER_BLOCK - 1
    ) -> tuple[Transaction, ...]:
        """Transactions à inclure dans le prochain bloc : applicables sur state, frais d'abord.

        max_transactions laisse par défaut une place pour la coinbase.
        """
        projected = _require_state(state)
        arrival = {tx_hash: rank for rank, tx_hash in enumerate(self._pending)}
        queues: dict[str, deque[Transaction]] = {}
        for transaction in self._pending.values():
            queues.setdefault(transaction.sender, deque()).append(transaction)
        heap: list[tuple[int, int, str]] = []
        for sender, queue in queues.items():
            queue_list = sorted(queue, key=lambda tx: (tx.sequence, arrival[tx.hash]))
            queues[sender] = deque(queue_list)
            head = queues[sender][0]
            heapq.heappush(heap, (-head.fee, arrival[head.hash], sender))
        selected: list[Transaction] = []
        while heap and len(selected) < max_transactions:
            _, _, sender = heapq.heappop(heap)
            queue = queues[sender]
            transaction = queue.popleft()
            try:
                projected = projected.apply_transaction(transaction)
                selected.append(transaction)
            except InvalidTransactionError:
                pass  # périmée ou pas finançable : on tente quand même la suivante du même expéditeur
            if queue:
                head = queue[0]
                heapq.heappush(heap, (-head.fee, arrival[head.hash], sender))
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

    def resync(self, state: State, extra: tuple[Transaction, ...] = ()) -> tuple[Transaction, ...]:
        """Rejoue l'admission de toutes les transactions en attente PUIS de extra sur state.

        Sert après une réorganisation de chaîne : state est le nouvel état,
        extra les transactions des blocs abandonnés (elles gardent leur
        chance d'être confirmées sur la nouvelle branche). Tout ce qui n'est
        plus applicable (déjà confirmé ailleurs, solde disparu) ou qui ne
        respecte pas notre politique de frais (M5) est purgé ; retourne les
        transactions purgées.
        """
        projected = _require_state(state)
        kept: dict[str, Transaction] = {}
        dropped: list[Transaction] = []
        for transaction in tuple(self._pending.values()) + tuple(extra):
            if transaction.hash in kept or transaction.is_coinbase:
                continue
            try:
                validate_transaction(transaction)
                if transaction.fee < self._min_fee:
                    raise InvalidTransactionError("frais sous le minimum relayé")
                projected = projected.apply_transaction(transaction)
            except InvalidTransactionError:
                dropped.append(transaction)
                continue
            if len(kept) >= self._max_size:
                dropped.append(transaction)
                continue
            kept[transaction.hash] = transaction
        self._pending = kept
        return tuple(dropped)

    def _project(self, state: State, skipped: frozenset[str] | set[str]) -> State:
        projected = _require_state(state)
        for tx_hash, transaction in self._pending.items():
            if tx_hash in skipped:
                continue
            try:
                projected = projected.apply_transaction(transaction)
            except InvalidTransactionError:
                continue  # devenue inapplicable : sera purgée par remove_confirmed()
        return projected

    def _cheapest(self) -> Transaction:
        """La transaction la moins payante ; à frais égal, la plus récente (on garde les premières arrivées)."""
        ranked = enumerate(self._pending.values())
        _, cheapest = min(ranked, key=lambda item: (item[1].fee, -item[0]))
        return cheapest

    def _dependents(self, transaction: Transaction) -> tuple[Transaction, ...]:
        """transaction et celles du même expéditeur de séquence supérieure (inapplicables sans elle)."""
        return tuple(
            pending
            for pending in self._pending.values()
            if pending.sender == transaction.sender and pending.sequence >= transaction.sequence
        )


def _require_state(state: object) -> State:
    if not isinstance(state, State):
        raise TypeError(f"objet State attendu, reçu {type(state).__name__}")
    return state
