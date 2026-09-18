"""Chaîne de blocs : conteneur ordonné, validation d'intégrité, état des comptes.

validate_chain() rejoue toutes les vérifications bloc par bloc, du Genesis au
dernier, sans faire confiance à AUCUNE valeur stockée : chaque hash est
recalculé, chaque preuve de travail vérifiée, chaque transaction appliquée à
l'état des comptes. C'est ce qui rend toute falsification détectable :

* une transaction modifiée sans recalcul => son hash stocké ne correspond plus ;
* recalculée mais non signée par l'expéditeur => signature invalide ;
* signée mais déjà confirmée (rejeu) ou hors séquence => refusée par l'état (S1) ;
* dépensant plus que le solde => refusée par l'état (S2) ;
* une coinbase gonflée => refusée par la règle B7 ;
* tout bloc réécrit doit être re-miné, et tous les suivants avec lui.

chain_work() mesure le travail cumulé. Ce sera le critère de choix entre deux
chaînes concurrentes (P2P) : la plus lourde en travail l'emporte.

La classe Blockchain maintient l'invariant « tous mes blocs forment une
chaîne valide » et l'État des comptes qui en découle, mis à jour à chaque
add_block() réussi. Un bloc refusé ne modifie ni les blocs ni l'état.

Index (Partie 12) : pour répondre vite à « où est cette transaction ? » et
« quelles transactions concernent cette adresse ? » (API HTTP, explorateur),
la chaîne tient deux index en mémoire, reconstruits avec elle : hash de
transaction -> (bloc, position) et adresse -> hashes des transactions où elle
est expéditeur ou destinataire, dans l'ordre de la chaîne. Ce ne sont que des
vues : rien de ce qui est hashé ou validé n'en dépend.
"""

from collections.abc import Sequence

from .block import Block, create_genesis_block, validate_block
from .errors import InvalidBlockError, InvalidChainError
from .proof_of_work import block_work
from .state import State
from .transaction import Transaction


def _replay(blocks: Sequence[Block]) -> State:
    """Valide la chaîne entière et retourne l'État des comptes final."""
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        raise InvalidChainError("séquence de blocs attendue")
    if len(blocks) == 0:
        raise InvalidChainError("chaîne vide : le bloc Genesis est obligatoire")
    if blocks[0] != create_genesis_block():
        raise InvalidChainError("bloc n°0 : ne correspond pas au Genesis canonique")
    state = State()
    for position in range(1, len(blocks)):
        try:
            validate_block(blocks[position], blocks[position - 1])
            state = state.apply_block(blocks[position])
        except InvalidBlockError as error:
            raise InvalidChainError(f"position {position} : {error}") from None
    return state


def validate_chain(blocks: Sequence[Block]) -> None:
    """Lève InvalidChainError si la suite de blocs n'est pas une chaîne intègre."""
    _replay(blocks)


def is_valid_chain(blocks: Sequence[Block]) -> bool:
    """Version booléenne de validate_chain()."""
    try:
        validate_chain(blocks)
    except InvalidChainError:
        return False
    return True


def compute_state(blocks: Sequence[Block]) -> State:
    """État des comptes après rejeu complet de la chaîne (qui doit être valide)."""
    return _replay(blocks)


def chain_work(blocks: Sequence[Block]) -> int:
    """Travail cumulé d'une suite de blocs (somme des difficultés)."""
    return sum(block_work(block.difficulty) for block in blocks)


class Blockchain:
    """Liste ordonnée de blocs commençant par le Genesis, toujours valide, avec son État."""

    def __init__(self) -> None:
        self._blocks: list[Block] = [create_genesis_block()]
        self._state = State()
        self._index_by_hash: dict[str, int] = {self._blocks[0].hash: 0}
        self._transaction_index: dict[str, tuple[int, int]] = {}  # hash de tx -> (index du bloc, position)
        self._by_address: dict[str, list[str]] = {}  # adresse -> hashes de tx, dans l'ordre de la chaîne

    @classmethod
    def from_blocks(cls, blocks: Sequence[Block]) -> "Blockchain":
        """Reconstruit une chaîne (reçue d'un pair, chargée d'un fichier) après validation complète."""
        state = _replay(blocks)
        chain = cls()
        chain._blocks = list(blocks)
        chain._state = state
        chain._index_by_hash = {block.hash: block.index for block in chain._blocks}
        chain._transaction_index = {}
        chain._by_address = {}
        for block in chain._blocks:
            chain._index_transactions(block)
        return chain

    @property
    def blocks(self) -> tuple[Block, ...]:
        """Vue en lecture seule (copie) des blocs."""
        return tuple(self._blocks)

    @property
    def last_block(self) -> Block:
        return self._blocks[-1]

    @property
    def state(self) -> State:
        """État des comptes après le dernier bloc (immuable)."""
        return self._state

    def __len__(self) -> int:
        return len(self._blocks)

    @property
    def height(self) -> int:
        """Index du dernier bloc (le Genesis seul donne 0)."""
        return self._blocks[-1].index

    def block_at(self, index: int) -> Block | None:
        """Bloc d'index donné, ou None s'il n'existe pas (encore)."""
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self._blocks):
            return None
        return self._blocks[index]

    def index_of(self, block_hash: str) -> int | None:
        """Index du bloc portant ce hash, ou None si la chaîne ne le contient pas."""
        return self._index_by_hash.get(block_hash)

    def __contains__(self, item: object) -> bool:
        """`block in chain` ou `hash in chain` : vrai si ce bloc fait partie de la chaîne."""
        key = item.hash if isinstance(item, Block) else item
        return key in self._index_by_hash

    def blocks_from(self, from_index: int, limit: int) -> tuple[Block, ...]:
        """Au plus limit blocs consécutifs à partir de from_index (vide si hors chaîne)."""
        if not isinstance(from_index, int) or isinstance(from_index, bool) or from_index < 0 or limit < 0:
            return ()
        return tuple(self._blocks[from_index : from_index + limit])

    def index_of_transaction(self, transaction_hash: str) -> int | None:
        """Index du bloc qui contient cette transaction, ou None si la chaîne ne la contient pas."""
        located = self._transaction_index.get(transaction_hash)
        return None if located is None else located[0]

    def find_transaction(self, transaction_hash: str) -> tuple[Block, Transaction] | None:
        """(bloc, transaction) pour ce hash, ou None si la chaîne ne la contient pas."""
        located = self._transaction_index.get(transaction_hash)
        if located is None:
            return None
        block = self._blocks[located[0]]
        return block, block.transactions[located[1]]

    def transaction_hashes_of(self, address: str) -> tuple[str, ...]:
        """Hashes des transactions où address est expéditeur ou destinataire, dans l'ordre de la chaîne."""
        return tuple(self._by_address.get(address, ()))

    def _index_transactions(self, block: Block) -> None:
        for position, transaction in enumerate(block.transactions):
            self._transaction_index[transaction.hash] = (block.index, position)
            if transaction.is_coinbase or transaction.sender == transaction.recipient:
                parties = (transaction.recipient,)
            else:
                parties = (transaction.sender, transaction.recipient)
            for address in parties:
                self._by_address.setdefault(address, []).append(transaction.hash)

    @property
    def total_work(self) -> int:
        """Travail cumulé de la chaîne (voir chain_work)."""
        return chain_work(self._blocks)

    def add_block(self, block: Block) -> None:
        """Ajoute block s'il est valide, chaîné au dernier bloc et accepté par l'État.

        Lève InvalidBlockError sinon, sans rien modifier.
        """
        validate_block(block, self.last_block)
        new_state = self._state.apply_block(block)
        self._blocks.append(block)
        self._state = new_state
        self._index_by_hash[block.hash] = block.index
        self._index_transactions(block)

    def validate(self) -> None:
        """Lève InvalidChainError si la chaîne n'est plus intègre."""
        validate_chain(self._blocks)

    def is_valid(self) -> bool:
        return is_valid_chain(self._blocks)
