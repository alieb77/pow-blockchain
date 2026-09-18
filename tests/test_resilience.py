"""Tests de la Partie 10 : résilience.

Rappel des pairs avec délai croissant (Node.tick, sans socket, horloge
factice), amorces jamais oubliées, oubli des adresses mortes, bannissement
temporaire des pairs fautifs ; le même comportement sur le réseau simulé
(hôtes distincts « sim-<id> ») et sur de vraies sockets.
"""

import asyncio
import socket
import unittest
from dataclasses import replace

import powchain.node as node_module
from powchain.block import GENESIS_TIMESTAMP, create_block
from powchain.codec import block_to_dict
from powchain.errors import ProtocolError
from powchain.network import NodeServer, local_ip_addresses
from powchain.node import (
    BAN_SECONDS,
    DIAL_GRACE_SECONDS,
    MAX_DIAL_FAILURES,
    RECONNECT_MAX_DELAY,
    AddressForgotten,
    Connect,
    Disconnect,
    Node,
    Send,
)
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.protocol import HELLO, NEW_BLOCK, PEERS, Message, decode_message, message
from powchain.simulation import FakeClock, SimulatedNetwork
from tests.helpers import MINER, mined
from tests.test_network import ServerFixture

START = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME


def connects(actions) -> list[str]:
    return [action.address for action in actions if isinstance(action, Connect)]


def only_disconnect(actions, pattern: str) -> Disconnect:
    (action,) = actions
    assert isinstance(action, Disconnect), actions
    assert pattern in action.reason, action.reason
    return action


def forged_block(node: Node, timestamp: int):
    """Un bloc correctement miné mais à récompense gonflée : invalide pour tout nœud honnête."""
    block = mined(create_block(node.tip, [], MINER.address, timestamp=timestamp))
    greedy = replace(block.coinbase, amount=block.coinbase.amount + 1)
    greedy = replace(greedy, hash=greedy.calculate_hash())
    return mined(replace(block, transactions=(greedy,)))


# ------------------------------------------------------------- rappel (tick)


class TickFixture(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.events = []
        self.node = Node(node_id="me", listen_port=5000, clock=self.clock, max_peers=3)
        self.node.add_listener(self.events.append)

    def fail_dial(self, address):
        """Le tick appelle `address`, le transport rapporte l'échec."""
        self.assertIn(address, connects(self.node.tick()))
        self.node.on_dial_failed(address)

    def handshake(self, peer_id, address, name="peer"):
        host, port = address.rsplit(":", 1)
        self.node.on_connect(peer_id, host, True, address)
        self.node.on_message(peer_id, Node(node_id=name, listen_port=int(port)).hello())


class ReconnectTests(TickFixture):
    def test_tick_dials_known_addresses_up_to_max_peers_most_recent_first(self):
        self.node.remember_addresses(["10.0.0.1:5000", "10.0.0.2:5000", "10.0.0.3:5000", "10.0.0.4:5000"])
        self.assertEqual(connects(self.node.tick()), ["10.0.0.4:5000", "10.0.0.3:5000", "10.0.0.2:5000"])
        self.assertEqual(self.node.tick(), [])  # appels en cours : rien de plus
        self.assertEqual(len(self.node.dialing), 3)
        self.assertEqual(self.node.stats["dials"], 3)

    def test_seeds_come_first(self):
        self.node.remember_addresses(["10.0.0.1:5000", "10.0.0.2:5000"])
        self.node.remember_addresses(["10.0.0.9:5000"], seed=True)
        self.assertEqual(connects(self.node.tick())[0], "10.0.0.9:5000")
        self.assertEqual(self.node.seed_addresses, ("10.0.0.9:5000",))

    def test_connected_own_and_banned_addresses_are_skipped(self):
        self.node.remember_addresses(["10.0.0.1:5000", "10.0.0.2:5000", "10.0.0.3:5000"])
        self.node.on_connect(1, "10.0.0.1", False)
        self.node.on_message(1, Node(node_id="p1", listen_port=5000).hello())  # déjà connecté (entrant)
        self.node.on_connect(2, "10.0.0.2", True, "10.0.0.2:5000")
        only_disconnect(self.node.on_message(2, self.node.hello()), "soi-même")  # c'est nous
        self.node.on_disconnect(2)
        self.node.on_connect(3, "10.0.0.3", False)
        only_disconnect(self.node.on_message(3, Message("dance", {})), "hors protocole")  # banni
        self.node.on_disconnect(3)
        self.assertEqual(connects(self.node.tick()), [])

    def test_failure_delays_grow_1_2_4_8(self):
        self.node.remember_addresses(["10.0.0.1:5000"])
        for delay in (1, 2, 4, 8):
            self.fail_dial("10.0.0.1:5000")
            self.assertEqual(self.node.retry_in("10.0.0.1:5000"), delay)
            self.assertEqual(self.node.tick(), [])  # pas tout de suite
            self.clock.advance(delay - 1)
            self.assertEqual(self.node.tick(), [])  # toujours pas
            self.clock.advance(1)
        self.assertEqual(connects(self.node.tick()), ["10.0.0.1:5000"])

    def test_delay_is_capped(self):
        self.node.remember_addresses(["10.0.0.1:5000"], seed=True)  # amorce : jamais oubliée
        for _ in range(20):
            self.fail_dial("10.0.0.1:5000")
            self.clock.advance(self.node.retry_in("10.0.0.1:5000"))
        self.fail_dial("10.0.0.1:5000")
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), RECONNECT_MAX_DELAY)

    def test_dead_address_is_forgotten_after_max_failures(self):
        self.node.remember_addresses(["10.0.0.1:5000"])
        for _ in range(MAX_DIAL_FAILURES):
            self.assertIn("10.0.0.1:5000", self.node.known_addresses)
            self.fail_dial("10.0.0.1:5000")
            self.clock.advance(RECONNECT_MAX_DELAY)
        self.assertNotIn("10.0.0.1:5000", self.node.known_addresses)
        self.assertIn(AddressForgotten("10.0.0.1:5000"), self.events)
        self.assertEqual(self.node.tick(), [])
        self.assertEqual(self.node.stats["dial_failures"], MAX_DIAL_FAILURES)

    def test_seed_is_never_forgotten(self):
        self.node.remember_addresses(["10.0.0.1:5000"], seed=True)
        for _ in range(MAX_DIAL_FAILURES + 5):
            self.fail_dial("10.0.0.1:5000")
            self.clock.advance(RECONNECT_MAX_DELAY)
        self.assertIn("10.0.0.1:5000", self.node.known_addresses)
        self.assertEqual(self.events, [])

    def test_seed_survives_address_book_eviction(self):
        self.node.remember_addresses(["10.9.9.9:5000"], seed=True)
        for i in range(node_module.MAX_KNOWN_ADDRESSES + 10):
            self.node.remember_addresses([f"10.1.{i // 250}.{i % 250}:1"])
        self.assertIn("10.9.9.9:5000", self.node.known_addresses)
        self.assertEqual(len(self.node.known_addresses), node_module.MAX_KNOWN_ADDRESSES)

    def test_successful_handshake_resets_failures(self):
        self.node.remember_addresses(["10.0.0.1:5000"])
        for _ in range(3):
            self.fail_dial("10.0.0.1:5000")
            self.clock.advance(self.node.retry_in("10.0.0.1:5000"))
        (connect,) = self.node.tick()
        self.handshake(1, connect.address)
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 0)
        self.assertEqual(self.node.dialing, ())
        self.node.on_disconnect(1)  # connexion perdue : rappel après 1 s, pas 16
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 1)
        self.clock.advance(1)
        self.assertEqual(connects(self.node.tick()), ["10.0.0.1:5000"])

    def test_remote_that_closes_before_hello_counts_as_failure(self):
        # ex. « déjà connecté à ce nœud » côté distant : sans délai croissant, on rappellerait chaque seconde
        self.node.remember_addresses(["10.0.0.1:5000"])
        (connect,) = self.node.tick()
        self.node.on_connect(1, "10.0.0.1", True, connect.address)
        self.node.on_disconnect(1)
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 1)
        self.clock.advance(1)
        (connect,) = self.node.tick()
        self.node.on_connect(2, "10.0.0.1", True, connect.address)
        self.node.on_disconnect(2)
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 2)

    def test_stale_dial_is_treated_as_failure(self):
        self.node.remember_addresses(["10.0.0.1:5000"])
        self.node.tick()
        self.clock.advance(DIAL_GRACE_SECONDS)
        self.assertEqual(self.node.tick(), [])  # réputé échoué : rappel dans 1 s
        self.assertEqual(self.node.dialing, ())
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 1)

    def test_skipped_dial_frees_the_slot_without_failure(self):
        self.node.remember_addresses(["10.0.0.1:5000"])
        self.node.tick()
        self.node.on_dial_skipped("10.0.0.1:5000")
        self.assertEqual(self.node.dialing, ())
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 0)
        self.assertEqual(connects(self.node.tick()), ["10.0.0.1:5000"])

    def test_discovery_dials_count_against_outbound_slots(self):
        self.node.on_connect(1, "10.0.0.1", False)
        self.node.on_message(1, Node(node_id="p", listen_port=6000).hello())
        actions = self.node.on_message(1, message(PEERS, addresses=["10.0.1.1:1", "10.0.1.2:1", "10.0.1.3:1", "10.0.1.4:1"]))
        self.assertEqual(len(connects(actions)), 3)
        self.assertEqual(self.node.tick(), [])  # tout est pris par les appels en cours
        self.assertIn("10.0.1.4:1", self.node.known_addresses)  # elle attendra un créneau

    def test_dial_failure_outside_the_book_is_ignored(self):
        self.node.on_dial_failed("10.0.0.1:5000")
        self.assertEqual(self.node.retry_in("10.0.0.1:5000"), 0)
        self.assertEqual(self.node.stats["dial_failures"], 0)

    def test_bad_seed_address_is_rejected(self):
        with self.assertRaises(ProtocolError):
            self.node.remember_addresses(["pas une adresse"], seed=True)


# ------------------------------------------------------------------- bans


class BanTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.node = Node(node_id="me", listen_port=5000, clock=self.clock)

    def misbehave(self, host="10.0.0.1", peer_id=1):
        self.node.on_connect(peer_id, host, False)
        action = only_disconnect(self.node.on_message(peer_id, Message("dance", {})), "hors protocole")
        self.node.on_disconnect(peer_id)
        return action

    def test_garbage_bans_the_host(self):
        self.misbehave()
        self.assertTrue(self.node.is_banned("10.0.0.1"))
        self.assertEqual(self.node.banned_hosts, ("10.0.0.1",))
        only_disconnect(self.node.on_connect(2, "10.0.0.1", False), "banni")
        self.assertIsNone(self.node.peer(2))
        self.assertEqual(self.node.stats["banned_refused"], 1)
        (action,) = self.node.on_connect(3, "10.0.0.2", False)  # un autre hôte passe
        self.assertIsInstance(action, Send)

    def test_ban_expires(self):
        self.misbehave()
        self.clock.advance(BAN_SECONDS - 1)
        self.assertTrue(self.node.is_banned("10.0.0.1"))
        self.clock.advance(1)
        self.assertFalse(self.node.is_banned("10.0.0.1"))
        self.node.tick()
        self.assertEqual(self.node.banned_hosts, ())
        (action,) = self.node.on_connect(2, "10.0.0.1", False)
        self.assertIsInstance(action, Send)

    def test_banned_host_is_not_dialed(self):
        self.node.remember_addresses(["10.0.0.1:5000"])
        self.misbehave()
        self.assertEqual(self.node.tick(), [])
        self.clock.advance(BAN_SECONDS)
        self.assertEqual(connects(self.node.tick()), ["10.0.0.1:5000"])

    def test_discovery_does_not_dial_a_banned_host_but_remembers_it(self):
        self.misbehave()
        self.node.on_connect(2, "10.0.0.2", False)
        self.node.on_message(2, Node(node_id="p", listen_port=6000).hello())
        self.assertEqual(self.node.on_message(2, message(PEERS, addresses=["10.0.0.1:7000"])), [])
        self.assertIn("10.0.0.1:7000", self.node.known_addresses)

    def test_invalid_block_bans(self):
        self.node.on_connect(1, "10.0.0.1", False)
        self.node.on_message(1, Node(node_id="p").hello())
        forged = forged_block(self.node, self.clock.now)
        only_disconnect(self.node.on_message(1, message(NEW_BLOCK, block=block_to_dict(forged))), "bloc invalide")
        self.assertTrue(self.node.is_banned("10.0.0.1"))

    def test_benign_disconnects_do_not_ban(self):
        other = Node(node_id="other", listen_port=6000)
        self.node.on_connect(1, "10.0.0.1", False)
        only_disconnect(self.node.on_message(1, Message(HELLO, {**other.hello().payload, "version": 99})), "version")
        self.node.on_connect(2, "10.0.0.2", True, "10.0.0.2:5000")
        only_disconnect(self.node.on_message(2, self.node.hello()), "soi-même")
        self.node.on_connect(3, "10.0.0.3", False)
        self.node.on_message(3, other.hello())
        self.node.on_connect(4, "10.0.0.4", False)
        only_disconnect(self.node.on_message(4, other.hello()), "déjà connecté")
        self.assertEqual(self.node.banned_hosts, ())
        self.assertEqual(self.node.stats["bans"], 0)

    def test_loopback_is_never_banned(self):
        self.misbehave(host="127.0.0.1")
        self.assertFalse(self.node.is_banned("127.0.0.1"))
        (action,) = self.node.on_connect(2, "127.0.0.1", False)
        self.assertIsInstance(action, Send)
        self.assertEqual(self.node.stats["punished"], 1)

    def test_transport_level_fault_via_punish(self):
        self.node.on_connect(1, "10.0.0.1", False)
        only_disconnect(self.node.punish(1, "ligne illisible"), "illisible")
        self.assertTrue(self.node.is_banned("10.0.0.1"))
        self.assertEqual(self.node.punish(42, "connexion inconnue"), [])

    def test_bans_are_bounded(self):
        for i in range(node_module.MAX_BANS + 5):
            self.misbehave(host=f"10.{i // 250}.{i % 250}.1", peer_id=i)
            self.clock.advance(1)
        self.assertEqual(len(self.node.banned_hosts), node_module.MAX_BANS)


# ------------------------------------------------------------- simulation


class SimulatedResilienceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.net = SimulatedNetwork(self.clock)
        self.a = self.net.add(Node(node_id="A", clock=self.clock))
        self.b = self.net.add(Node(node_id="B", clock=self.clock))
        self.net.connect("A", "B")

    def test_lost_peer_is_called_back_when_it_returns(self):
        b_address = self.net.address_of("B")
        self.net.disconnect("A", "B")
        self.net.partition("A", "B")  # B est éteint
        self.assertEqual(self.a.retry_in(b_address), 1)
        self.clock.advance(1)
        self.net.tick("A")  # appel -> échec
        self.assertFalse(self.net.connected("A", "B"))
        self.assertEqual(self.a.retry_in(b_address), 2)
        self.net.heal("A", "B")  # B revient
        self.clock.advance(1)
        self.net.tick("A")  # trop tôt
        self.assertFalse(self.net.connected("A", "B"))
        self.clock.advance(1)
        self.net.tick("A")
        self.assertTrue(self.net.connected("A", "B"))
        self.assertEqual({p.node_id for p in self.a.peers}, {"B"})
        self.assertEqual(self.a.retry_in(b_address), 0)

    def test_unknown_address_fails_and_is_eventually_forgotten(self):
        self.a.remember_addresses(["sim-Z:10099"])
        for _ in range(MAX_DIAL_FAILURES):
            self.net.tick("A")
            self.clock.advance(RECONNECT_MAX_DELAY)
        self.assertNotIn("sim-Z:10099", self.a.known_addresses)

    def test_faulty_peer_is_banned_in_the_simulation_too(self):
        self.net.add(Node(node_id="C", clock=self.clock))
        self.net.connect("C", "A")
        forged = forged_block(self.a, self.clock.now)
        self.net.run("C", [Send(self.net.peer_id("C", "A"), message(NEW_BLOCK, block=block_to_dict(forged)))])
        self.assertFalse(self.net.connected("A", "C"))
        self.assertTrue(self.a.is_banned(self.net.host_of("C")))
        self.assertTrue(self.net.connected("A", "B"))  # B a son propre hôte : pas concerné
        self.net.connect("C", "A")
        self.assertFalse(self.net.connected("A", "C"))  # refusé avant hello
        self.clock.advance(BAN_SECONDS)
        self.net.tick("A")
        self.net.connect("C", "A")
        self.assertTrue(self.net.connected("A", "C"))


# ----------------------------------------------------------------- sockets


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ResilientSocketFixture(ServerFixture):
    async def start(self, name, *, host="127.0.0.1", port=0, **node_kwargs):
        node = Node(node_id=name, **node_kwargs)
        server = NodeServer(node, host=host, port=port, mining_chunk=512, tick_interval=0.05, dial_timeout=2.0)
        await server.start()
        self.servers.append(server)
        return server


class ReconnectOverSocketsTests(ResilientSocketFixture):
    async def test_peer_that_appears_later_is_reached(self):
        a = await self.start("A")
        port = free_port()
        address = f"127.0.0.1:{port}"
        a.node.remember_addresses([address], seed=True)
        await a.tick_now()  # personne n'écoute encore
        self.assertTrue(await a.wait_until(lambda: a.node.retry_in(address) >= 1, timeout=3))
        await self.start("B", port=port)
        self.assertTrue(await a.wait_until(lambda: {p.node_id for p in a.node.peers} == {"B"}, timeout=8))
        self.assertEqual(a.node.retry_in(address), 0)

    async def test_lost_peer_is_called_back(self):
        a, b = await self.start("A"), await self.start("B")
        port = b.port
        self.assertTrue(await a.connect(b.address))
        self.assertTrue(await a.wait_until(lambda: len(a.node.peers) == 1))
        await b.stop()
        self.servers.remove(b)
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0))
        await self.start("B2", port=port)
        self.assertTrue(await a.wait_until(lambda: {p.node_id for p in a.node.peers} == {"B2"}, timeout=8))


LAN_IPS = local_ip_addresses()


@unittest.skipUnless(LAN_IPS, "aucune adresse réseau locale : test de ban impossible")
class BanOverSocketsTests(ResilientSocketFixture):
    async def test_garbage_from_a_lan_host_bans_it_but_not_loopback(self):
        lan_ip = LAN_IPS[0]
        a = await self.start("A", host="0.0.0.0")
        reader, writer = await asyncio.open_connection(lan_ip, a.port)
        self.addAsyncCleanup(self._close, writer)
        writer.write(b"pas du json\n")
        await writer.drain()
        received = await asyncio.wait_for(reader.read(), 5)
        self.assertEqual(received.count(b"\n"), 1)  # son hello, puis fermeture
        self.assertTrue(await a.wait_until(lambda: a.node.is_banned(lan_ip)))
        reader2, writer2 = await asyncio.open_connection(lan_ip, a.port)
        self.addAsyncCleanup(self._close, writer2)
        self.assertEqual(await asyncio.wait_for(reader2.readline(), 5), b"")  # refusé avant hello
        reader3, writer3 = await asyncio.open_connection("127.0.0.1", a.port)
        self.addAsyncCleanup(self._close, writer3)
        self.assertEqual(decode_message(await asyncio.wait_for(reader3.readline(), 5)).type, HELLO)  # la boucle locale passe


if __name__ == "__main__":
    unittest.main()
