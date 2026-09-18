"""Représentation monétaire.

Décision : un montant est un entier Python (précision arbitraire, jamais de
float) exprimé dans la plus petite unité indivisible de la monnaie.

    1 COIN = 100_000_000 unités        (même convention que 1 BTC = 10^8 satoshis)

Pourquoi pas de float ? En flottant, 0.1 + 0.2 != 0.3 : deux machines
pourraient obtenir des montants différents, donc des hashes différents.
Avec des entiers, 1 + 2 == 3 partout, toujours.

Bornes : un montant valide vérifie 0 <= amount <= MAX_MONEY. MAX_MONEY vaut
21 000 000 COIN, soit 2.1e15 unités, ce qui tient largement dans les
8 octets non signés (uint64, max ~1.8e19) de la sérialisation canonique.
"""

COIN_SYMBOL = "COIN"
COIN_DECIMALS = 8
UNITS_PER_COIN = 10**COIN_DECIMALS
MAX_SUPPLY_COINS = 21_000_000
MAX_MONEY = MAX_SUPPLY_COINS * UNITS_PER_COIN


def is_valid_amount(value: object) -> bool:
    """Vrai si value est un entier (pas un bool) compris entre 0 et MAX_MONEY."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_MONEY


def _is_ascii_digits(text: str) -> bool:
    return text != "" and all("0" <= character <= "9" for character in text)


def parse_coin_amount(text: str) -> int:
    """Convertit une écriture décimale en COIN ("1.5", "0.00000001") en unités.

    Le parsing est purement textuel : aucun flottant n'intervient.
    """
    if not isinstance(text, str):
        raise TypeError("parse_coin_amount attend une chaîne, par exemple '1.5'")
    whole_part, separator, fraction_part = text.strip().partition(".")
    if not _is_ascii_digits(whole_part) or (separator and not _is_ascii_digits(fraction_part)):
        raise ValueError(f"montant décimal invalide : {text!r}")
    if len(fraction_part) > COIN_DECIMALS:
        raise ValueError(f"au plus {COIN_DECIMALS} décimales autorisées : {text!r}")
    fraction_units = int(fraction_part.ljust(COIN_DECIMALS, "0")) if fraction_part else 0
    return int(whole_part) * UNITS_PER_COIN + fraction_units


def format_units(units: int) -> str:
    """Affiche un montant en unités sous la forme '1.50000000 COIN'."""
    if not isinstance(units, int) or isinstance(units, bool) or units < 0:
        raise ValueError(f"format_units attend un entier >= 0, reçu {units!r}")
    whole, fraction = divmod(units, UNITS_PER_COIN)
    return f"{whole}.{fraction:0{COIN_DECIMALS}d} {COIN_SYMBOL}"


# Émission monétaire (Partie 4). La seule façon de créer des pièces est la
# transaction coinbase d'un bloc miné, qui vaut block_reward(hauteur). La
# récompense est divisée par deux tous les HALVING_INTERVAL blocs, comme
# Bitcoin. Somme de toutes les récompenses :
#     210 000 * 50 * (1 + 1/2 + 1/4 + ...) < 210 000 * 100 = 21 000 000 COIN
# soit strictement moins que MAX_MONEY : le plafond n'est jamais atteint.
INITIAL_BLOCK_REWARD = 50 * UNITS_PER_COIN
HALVING_INTERVAL = 210_000


def block_reward(height: int) -> int:
    """Récompense (en unités) créée par la coinbase du bloc de hauteur height (>= 1)."""
    if not isinstance(height, int) or isinstance(height, bool) or height < 1:
        raise ValueError(f"hauteur de bloc >= 1 attendue, reçu {height!r}")
    return INITIAL_BLOCK_REWARD >> (height // HALVING_INTERVAL)
