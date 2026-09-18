"""Clés de test déterministes et fabrique de transactions signées.

Les clés sont dérivées de noms lisibles pour que les tests soient
reproductibles (mêmes adresses, mêmes hashes, mêmes signatures d'une
exécution à l'autre). Ce procédé est ÉVIDEMMENT interdit en production :
une vraie clé vient de KeyPair.generate().
"""

import hashlib

from powchain.keys import KeyPair
from powchain.transaction import Transaction, create_signed_transaction


def test_key(name: str) -> KeyPair:
    return KeyPair.from_seed(hashlib.sha256(f"powchain-test-key:{name}".encode("utf-8")).digest())


ALICE = test_key("alice")
BOB = test_key("bob")
CAROL = test_key("carol")
DAVE = test_key("dave")
MALLORY = test_key("mallory")


def signed_tx(
    sender: KeyPair, recipient: KeyPair, amount: int, data: str = "", sequence: int = 0
) -> Transaction:
    return create_signed_transaction(sender, recipient.address, amount, data, sequence)
