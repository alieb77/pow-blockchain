"""Règles de la preuve de travail (Proof of Work) : cible, difficulté, ajustement.

Idée en une phrase : pour qu'un bloc soit accepté, son hash, lu comme un
nombre de 256 bits, doit être inférieur ou égal à une CIBLE. Comme SHA-256
est imprévisible, la seule façon d'y parvenir est d'essayer des nonces
différents jusqu'à tomber sur un hash assez petit : une loterie où chaque
essai est un ticket. Ce module définit les règles ; mining.py joue à la
loterie.

Difficulté et cible
-------------------
    target(difficulty) = MAX_TARGET // difficulty        avec MAX_TARGET = 2**256 - 1

    difficulty = 1     -> tout hash convient (1 essai en moyenne)
    difficulty = 4096  -> 1 hash sur 4096 convient (4096 essais en moyenne)

La difficulté EST donc le nombre moyen d'essais nécessaires. C'est la
convention de Bitcoin (difficulty = max_target / target), en plus lisible.
Un hash « commence par des zéros » quand il est petit : à difficulté
4096 = 16^3, la cible commence par 3 zéros hexadécimaux, donc le hash aussi.

Alternative écartée : compter les zéros en tête du hash. Plus simple à lire
mais chaque zéro supplémentaire multiplie la difficulté par 16 : impossible
d'ajuster finement. La cible numérique permet des pas de +12,5 % / -12,5 %.

Ajustement de la difficulté
---------------------------
Objectif : un bloc toutes les TARGET_BLOCK_TIME secondes en moyenne, quelle
que soit la puissance de calcul des mineurs. Règle appliquée à chaque bloc à
partir du temps écoulé depuis le bloc précédent :

    plus rapide que la cible -> difficulté + difficulté / ADJUSTMENT_DIVISOR
    plus lent que la cible   -> difficulté - difficulté / ADJUSTMENT_DIVISOR
    exactement la cible      -> inchangée
    puis bornage dans [MIN_DIFFICULTY, MAX_DIFFICULTY]

La difficulté attendue d'un bloc ne dépend que du bloc précédent et du
timestamp du nouveau bloc : un validateur la recalcule avec ces deux seules
informations. C'est pourquoi elle est stockée dans l'en-tête (et hashée) puis
vérifiée par validate_block(block, prev_block).

Alternative écartée pour l'instant : l'ajustement par époque de Bitcoin (tous
les 2016 blocs, sur la durée totale de l'époque). Plus stable face au hasard
d'un seul bloc, mais il exige de remonter 2016 blocs en arrière. Ce sera un
raffinement possible quand la chaîne sera persistée.

Limite connue : un mineur peut retarder son timestamp pour obtenir -12,5 %
de difficulté. Les vraies chaînes bornent l'avance d'horloge acceptée par le
réseau (2 h pour Bitcoin) ; cette règle réseau viendra avec le P2P.

Travail (work)
--------------
Le travail d'un bloc = son nombre moyen d'essais = sa difficulté. Le travail
d'une chaîne = la somme des travaux de ses blocs. Quand deux chaînes
concurrentes coexisteront (P2P), la règle sera de retenir celle qui a le
plus de travail cumulé, pas la plus longue.
"""

from .crypto import is_valid_hash_hex

MAX_TARGET = 2**256 - 1

INITIAL_DIFFICULTY = 4096  # difficulté du Genesis : ~4096 essais, quelques ms en Python
MIN_DIFFICULTY = 1
MAX_DIFFICULTY = 2**64 - 1  # borne de l'encodage uint64 du champ difficulty
TARGET_BLOCK_TIME = 10  # secondes visées entre deux blocs
ADJUSTMENT_DIVISOR = 8  # pas d'ajustement : 1/8 = 12,5 % par bloc


def is_valid_difficulty(value: object) -> bool:
    """Vrai si value est un entier (pas un bool) dans [MIN_DIFFICULTY, MAX_DIFFICULTY]."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_DIFFICULTY <= value <= MAX_DIFFICULTY
    )


def target_from_difficulty(difficulty: int) -> int:
    """Cible numérique : le hash (en tant qu'entier) doit être <= à cette valeur."""
    if not is_valid_difficulty(difficulty):
        raise ValueError(f"difficulté invalide : {difficulty!r}")
    return MAX_TARGET // difficulty


def format_target(target: int) -> str:
    """Cible en hexadécimal sur 64 caractères, comparable visuellement à un hash."""
    return f"{target:064x}"


def hash_meets_target(hash_hex: str, difficulty: int) -> bool:
    """Vrai si le hash, lu comme un entier 256 bits, est <= à la cible de cette difficulté."""
    if not is_valid_hash_hex(hash_hex):
        return False
    return int(hash_hex, 16) <= target_from_difficulty(difficulty)


def expected_difficulty(prev_difficulty: int, prev_timestamp: int, timestamp: int) -> int:
    """Difficulté que DOIT porter un bloc daté timestamp après un bloc (prev_difficulty, prev_timestamp)."""
    if not is_valid_difficulty(prev_difficulty):
        raise ValueError(f"difficulté précédente invalide : {prev_difficulty!r}")
    elapsed = timestamp - prev_timestamp
    if elapsed < 1:
        raise ValueError("le timestamp doit être strictement supérieur à celui du bloc précédent")
    step = max(prev_difficulty // ADJUSTMENT_DIVISOR, 1)
    if elapsed < TARGET_BLOCK_TIME:
        adjusted = prev_difficulty + step
    elif elapsed > TARGET_BLOCK_TIME:
        adjusted = prev_difficulty - step
    else:
        adjusted = prev_difficulty
    return min(max(adjusted, MIN_DIFFICULTY), MAX_DIFFICULTY)


def block_work(difficulty: int) -> int:
    """Travail d'un bloc = nombre moyen d'essais qu'il a coûté = sa difficulté."""
    if not is_valid_difficulty(difficulty):
        raise ValueError(f"difficulté invalide : {difficulty!r}")
    return difficulty
