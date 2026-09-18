"""Exceptions du projet powchain.

Hiérarchie :

    PowChainError                 racine commune (permet un « except PowChainError »)
    ├── SerializationError        une donnée ne peut pas être mise au format canonique
    │                             (mauvais type, entier hors bornes, None...)
    ├── ValidationError           une donnée est bien formée mais viole une règle métier
    │   ├── InvalidTransactionError
    │   ├── InvalidBlockError
    │   └── InvalidChainError
    ├── MiningError               le minage n'a pas pu produire de preuve de travail
    │   └── MiningLimitError      la limite d'essais demandée a été atteinte
    └── MempoolError              refus propre à la file d'attente (plein, doublon, coinbase)

Chaque exception porte un message explicite décrivant la règle violée :
les fonctions is_valid_*() les convertissent en booléen, les fonctions
validate_*() les laissent remonter pour que l'appelant connaisse la raison.
"""


class PowChainError(Exception):
    """Classe de base de toutes les erreurs du projet."""


class SerializationError(PowChainError):
    """Impossible de produire la représentation canonique d'une donnée."""


class ValidationError(PowChainError):
    """Une règle de validité est violée."""


class InvalidTransactionError(ValidationError):
    """La transaction viole une règle (adresse, montant, data, hash...)."""


class InvalidBlockError(ValidationError):
    """Le bloc viole une règle (hash, chaînage, index, transactions...)."""


class InvalidChainError(ValidationError):
    """La chaîne viole une règle (genesis, lien entre blocs...)."""


class MiningError(PowChainError):
    """Le minage n'a pas pu aboutir (candidat mal formé, nonces épuisés...)."""


class MiningLimitError(MiningError):
    """Aucun nonce valide trouvé dans la limite d'essais demandée."""


class MempoolError(PowChainError):
    """Le mempool refuse une transaction pour une raison propre à la file.

    Les transactions invalides en elles-mêmes (signature, séquence, solde)
    lèvent InvalidTransactionError ; MempoolError couvre le reste : file
    pleine, doublon, coinbase soumise par un utilisateur.
    """
