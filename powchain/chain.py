"""Chaîne de blocs : conteneur ordonné et validation d'intégrité.

validate_chain() est la fonction centrale de cette étape : elle rejoue toutes
les vérifications bloc par bloc, du Genesis au dernier, sans faire confiance à
AUCUNE valeur stockée (chaque hash est recalculé). C'est ce qui rend toute
falsification détectable :

* une transaction modifiée sans recalcul => son hash stocké ne correspond plus ;
* si l'attaquant recalcule le hash de la transaction => le hash du bloc ne
  correspond plus ;
* s'il recalcule aussi le hash du bloc => le prev_hash du bloc SUIVANT ne
  correspond plus ;
* s'il recalcule TOUS les blocs suivants => depuis la Partie 2, chacun de ces
  blocs doit aussi être RE-MINÉ (règle B6) : réécrire l'historique coûte
  autant de travail que la chaîne honnête en a accumulé après le bloc
  falsifié, pendant que les mineurs honnêtes continuent d'en ajouter.

chain_work() mesure ce travail cumulé. Ce sera le critère de choix entre deux
chaînes concurrentes (P2P) : la plus lourde en travail l'emporte.

La classe Blockchain est un conteneur pratique qui maintient l'invariant
« tous mes blocs forment une chaîne valide » : on ne peut y ajouter un bloc
qu'après validation. Elle sera enrichie plus tard (mempool, soldes).
"""

from collections.abc import Sequence

from .block import Block, create_genesis_block, validate_block
from .errors import InvalidBlockError, InvalidChainError
from .proof_of_work import block_work


def validate_chain(blocks: Sequence[Block]) -> None:
    """Lève InvalidChainError si la suite de blocs n'est pas une chaîne intègre."""
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        raise InvalidChainError("séquence de blocs attendue")
    if len(blocks) == 0:
        raise InvalidChainError("chaîne vide : le bloc Genesis est obligatoire")
    if blocks[0] != create_genesis_block():
        raise InvalidChainError("bloc n°0 : ne correspond pas au Genesis canonique")
    for position in range(1, len(blocks)):
        try:
            validate_block(blocks[position], blocks[position - 1])
        except InvalidBlockError as error:
            raise InvalidChainError(f"position {position} : {error}") from None


def is_valid_chain(blocks: Sequence[Block]) -> bool:
    """Version booléenne de validate_chain()."""
    try:
        validate_chain(blocks)
    except InvalidChainError:
        return False
    return True


def chain_work(blocks: Sequence[Block]) -> int:
    """Travail cumulé d'une suite de blocs (somme des difficultés)."""
    return sum(block_work(block.difficulty) for block in blocks)


class Blockchain:
    """Liste ordonnée de blocs commençant par le Genesis, toujours valide."""

    def __init__(self) -> None:
        self._blocks: list[Block] = [create_genesis_block()]

    @classmethod
    def from_blocks(cls, blocks: Sequence[Block]) -> "Blockchain":
        """Reconstruit une chaîne (ex. chargée depuis un fichier) après validation complète."""
        validate_chain(blocks)
        chain = cls()
        chain._blocks = list(blocks)
        return chain

    @property
    def blocks(self) -> tuple[Block, ...]:
        """Vue en lecture seule (copie) des blocs."""
        return tuple(self._blocks)

    @property
    def last_block(self) -> Block:
        return self._blocks[-1]

    def __len__(self) -> int:
        return len(self._blocks)

    @property
    def total_work(self) -> int:
        """Travail cumulé de la chaîne (voir chain_work)."""
        return chain_work(self._blocks)

    def add_block(self, block: Block) -> None:
        """Ajoute block s'il est valide et chaîné au dernier bloc, sinon lève InvalidBlockError."""
        validate_block(block, self.last_block)
        self._blocks.append(block)

    def validate(self) -> None:
        """Lève InvalidChainError si la chaîne n'est plus intègre."""
        validate_chain(self._blocks)

    def is_valid(self) -> bool:
        return is_valid_chain(self._blocks)
