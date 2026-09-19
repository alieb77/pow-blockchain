"""Limite de débit par pair (seau à jetons) : anti-spam avant l'ouverture publique.

Le ban punit déjà les messages *invalides* (test_resilience.py). Ici on vérifie
qu'un pair qui inonde de messages *valides* (découverte, get_account…) vide son
seau à jetons et se fait déconnecter et bannir comme n'importe quel fautif, que
la boucle locale n'est jamais bannie, que le seau se remplit avec le temps
(débit soutenu autorisé), et que la limite se désactive avec message_rate=0.
"""

import unittest

from powchain.node import MESSAGE_BURST, MESSAGE_RATE, Disconnect, Node, Send
from powchain.protocol import PEERS, message
from powchain.simulation import FakeClock
from tests.helpers import MINER

START = 1_700_000_000


def peer_hello(node_id: str = "peer", listen_port: int = 6000) -> object:
    return Node(node_id=node_id, listen_port=listen_port).hello()


class RateLimitFixture(unittest.TestCase):
    def make_node(self, *, burst: float = 5, rate: float = 2) -> Node:
        self.clock = FakeClock(START)
        return Node(node_id="me", listen_port=5000, clock=self.clock, message_burst=burst, message_rate=rate)

    def connect(self, node: Node, peer_id: int = 1, host: str = "10.0.0.1") -> None:
        (action,) = node.on_connect(peer_id, host, False)  # notre hello sortant, pas encore de message reçu
        self.assertIsInstance(action, Send)

    @staticmethod
    def benign(node: Node, peer_id: int):
        """Un message valide et sans effet (découverte à liste vide) : consomme un jeton."""
        return node.on_message(peer_id, message(PEERS, addresses=[]))

    def flood(self, node: Node, peer_id: int = 1, limit: int = 50) -> bool:
        """Envoie des messages valides jusqu'à une déconnexion ; True si elle survient."""
        for _ in range(limit):
            actions = self.benign(node, peer_id)
            if any(isinstance(a, Disconnect) for a in actions):
                return True
        return False


class BurstAndBanTests(RateLimitFixture):
    def test_a_full_burst_is_tolerated(self):
        node = self.make_node(burst=5, rate=2)
        self.connect(node)
        node.on_message(1, peer_hello())  # hello : 1er jeton (seau 5 -> 4)
        for _ in range(4):  # 4 messages de plus vident exactement le seau, sans faute
            self.assertEqual([a for a in self.benign(node, 1) if isinstance(a, Disconnect)], [])
        self.assertEqual(node.stats["rate_limited"], 0)
        self.assertEqual(node.stats["bans"], 0)

    def test_flood_disconnects_and_bans_the_host(self):
        node = self.make_node(burst=5, rate=2)
        self.connect(node, host="10.0.0.1")
        node.on_message(1, peer_hello())
        self.assertTrue(self.flood(node), "un flot de messages valides doit finir déconnecté")
        self.assertTrue(node.is_banned("10.0.0.1"))
        self.assertEqual(node.banned_hosts, ("10.0.0.1",))
        self.assertGreaterEqual(node.stats["rate_limited"], 1)
        self.assertEqual(node.stats["bans"], 1)
        # Le message qui a fait déborder n'est pas traité : une nouvelle connexion est refusée (banni).
        only = node.on_connect(2, "10.0.0.1", False)
        self.assertEqual(len(only), 1)
        self.assertIsInstance(only[0], Disconnect)

    def test_loopback_flood_disconnects_but_never_bans(self):
        node = self.make_node(burst=5, rate=2)
        self.connect(node, host="127.0.0.1")
        node.on_message(1, peer_hello())
        self.assertTrue(self.flood(node))
        self.assertFalse(node.is_banned("127.0.0.1"))
        self.assertEqual(node.stats["bans"], 0)
        self.assertGreaterEqual(node.stats["punished"], 1)


class RefillTests(RateLimitFixture):
    def test_bucket_refills_over_time(self):
        node = self.make_node(burst=5, rate=2)
        self.connect(node)
        node.on_message(1, peer_hello())  # seau -> 4
        for _ in range(4):  # seau -> 0
            self.benign(node, 1)
        self.clock.advance(3)  # +6 jetons, plafonné à 5 : le seau est de nouveau plein
        for _ in range(5):
            self.assertEqual([a for a in self.benign(node, 1) if isinstance(a, Disconnect)], [])
        self.assertFalse(node.is_banned("10.0.0.1"))

    def test_sustained_rate_below_limit_never_trips(self):
        node = self.make_node(burst=5, rate=2)
        self.connect(node)
        node.on_message(1, peer_hello())
        for _ in range(30):  # 1 message par seconde, sous le débit de 2/s : jamais de faute
            self.clock.advance(1)
            self.assertEqual([a for a in self.benign(node, 1) if isinstance(a, Disconnect)], [])
        self.assertEqual(node.stats["rate_limited"], 0)


class DisabledTests(RateLimitFixture):
    def test_rate_zero_disables_the_limit(self):
        node = self.make_node(burst=5, rate=0)
        self.connect(node)
        node.on_message(1, peer_hello())
        self.assertFalse(self.flood(node, limit=500))
        self.assertEqual(node.stats["rate_limited"], 0)


class DefaultsTests(unittest.TestCase):
    def test_defaults_are_generous_enough_for_honest_peers(self):
        # La rafale par défaut dépasse largement la découverte et le rattrapage normaux.
        self.assertGreater(MESSAGE_BURST, 64)
        self.assertGreater(MESSAGE_RATE, 0)

    def test_a_new_peer_starts_with_a_full_bucket(self):
        node = Node(node_id="me", listen_port=5000)
        node.on_connect(1, "10.0.0.2", False)
        peer = node.peer(1)
        self.assertEqual(peer.tokens, MESSAGE_BURST)


if __name__ == "__main__":
    unittest.main()
