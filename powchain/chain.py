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
"""

from collections.abc import Sequence

from .block import Block, create_genesis_block, validate_block
from .errors import InvalidBlockError, InvalidChainError
from .proof_of_work import block_work
from .state import State


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

    @classmethod
    def from_blocks(cls, blocks: Sequence[Block]) -> "Blockchain":
        """Reconstruit une chaîne (ex. chargée depuis un fichier) après validation complète."""
        state = _replay(blocks)
        chain = cls()
        chain._blocks = list(blocks)
        chain._state = state
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

    def validate(self) -> None:
        """Lève InvalidChainError si la chaîne n'est plus intègre."""
        validate_chain(self._blocks)

    def is_valid(self) -> bool:
        return is_valid_chain(self._blocks)
