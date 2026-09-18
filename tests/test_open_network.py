"""Tests de la Partie 9 : ouverture au réseau.

Portée des adresses (protocol.py) ; ce qu'un nœud annonce et accepte selon la
portée, adresse propre apprise, plafond de connexions entrantes (node.py, sans
socket) ; délai de hello et plafond sur de vraies sockets, y compris via l'IP
réseau de la machine (network.py) ; carnet réécrit quand une adresse en sort
(storage.py) ; option --public (CLI).
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import powchain.node as node_module
from powchain.__main__ import build_parser, listen_host
from powchain.network import HELLO_TIMEOUT_SECONDS, NodeServer, local_ip_addresses
from powchain.node import AddressForgotten, AddressLearned, Connect, Disconnect, Node, Send
from powchain.protocol import (
    HELLO,
    LOOPBACK,
    PEERS,
    PRIVATE,
    PUBLIC,
    decode_message,
    encode_message,
    host_reaches,
    host_scope,
    message,
)
from powchain.simulation import SimulatedNetwork
from powchain.storage import NodeStorage
from tests.test_network import ServerFixture


def peers_sent(actions) -> list[str] | None:
    """Adresses du message « peers » parmi les actions, ou None si aucun n'a été envoyé."""
    for action in actions:
        if isinstance(action, Send) and action.message.type == PEERS:
            return action.message["addresses"]
    return None


def only_disconnect(actions, pattern: str) -> Disconnect:
    (action,) = actions
    assert isinstance(action, Disconnect), actions
    assert pattern in action.reason, action.reason
    return action


def client_hello(name: str, listen_port: int | None = None):
    return Node(node_id=name, listen_port=listen_port).hello()


# ----------------------------------------------------------------- protocol


class ScopeTests(unittest.TestCase):
    def test_loopback(self):
        for host in ("127.0.0.1", "127.0.0.2", "localhost", "::1", "0.0.0.0"):
            self.assertEqual(host_scope(host), LOOPBACK, host)

    def test_private_or_not_routable_on_the_internet(self):
        for host in ("192.168.1.9", "10.0.0.1", "172.16.5.5", "169.254.1.1", "100.64.0.1", "fe80::1", "fc00::1", "203.0.113.7"):
            self.assertEqual(host_scope(host), PRIVATE, host)

    def test_public_and_hostnames(self):
        for host in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "example.org", "sim"):
            self.assertEqual(host_scope(host), PUBLIC, host)

    def test_an_address_reaches_peers_at_least_as_close_as_its_scope(self):
        cases = {
            ("127.0.0.1", "127.0.0.1"): True,
            ("127.0.0.1", "192.168.1.5"): False,
            ("127.0.0.1", "8.8.8.8"): False,
            ("192.168.1.9", "127.0.0.1"): True,
            ("192.168.1.9", "192.168.1.5"): True,
            ("192.168.1.9", "8.8.8.8"): False,
            ("8.8.8.8", "127.0.0.1"): True,
            ("8.8.8.8", "192.168.1.5"): True,
            ("8.8.8.8", "1.1.1.1"): True,
            ("sim", "sim"): True,
        }
        for (host, peer_host), expected in cases.items():
            self.assertEqual(host_reaches(host, peer_host), expected, (host, peer_host))


# --------------------------------------------------------------------- node


class SharingByScopeTests(unittest.TestCase):
    """Ce qu'un nœud annonce à un pair dépend de là où ce pair se trouve."""

    def setUp(self):
        self.node = Node(node_id="me", listen_port=5000)
        self.node.remember_addresses(["127.0.0.1:5001", "192.168.1.20:5000", "8.8.8.8:5000"])

    def shared_with(self, host):
        self.node.on_connect(1, host, False)
        return peers_sent(self.node.on_message(1, client_hello("p")))

    def test_same_machine_gets_everything(self):
        self.assertEqual(self.shared_with("127.0.0.1"), ["127.0.0.1:5001", "192.168.1.20:5000", "8.8.8.8:5000"])

    def test_lan_peer_does_not_get_loopback(self):
        self.assertEqual(self.shared_with("192.168.1.30"), ["192.168.1.20:5000", "8.8.8.8:5000"])

    def test_internet_peer_gets_only_public(self):
        self.assertEqual(self.shared_with("1.1.1.1"), ["8.8.8.8:5000"])

    def test_nothing_shareable_means_no_peers_message(self):
        node = Node(node_id="me", listen_port=5000)
        node.remember_addresses(["127.0.0.1:5001"])
        node.on_connect(1, "1.1.1.1", False)
        self.assertIsNone(peers_sent(node.on_message(1, client_hello("p"))))

    def test_inbound_lan_peer_is_remembered_by_its_lan_address(self):
        self.node.on_connect(1, "192.168.1.30", False)
        self.node.on_message(1, client_hello("p", listen_port=6000))
        self.assertIn("192.168.1.30:6000", self.node.known_addresses)


class ReceivingByScopeTests(unittest.TestCase):
    """Une adresse plus locale que le pair qui l'envoie désigne SA machine ou SON réseau : ignorée."""

    def connected_from(self, host, **kwargs):
        node = Node(node_id="me", listen_port=5000, **kwargs)
        node.on_connect(1, host, False)
        node.on_message(1, client_hello("p", listen_port=6000))
        return node

    def test_internet_peer_addresses_narrower_than_itself_are_ignored(self):
        node = self.connected_from("1.1.1.1")
        actions = node.on_message(1, message(PEERS, addresses=["127.0.0.1:7000", "10.0.0.7:5000", "9.9.9.9:5000"]))
        self.assertEqual(actions, [Connect("9.9.9.9:5000")])
        self.assertNotIn("127.0.0.1:7000", node.known_addresses)
        self.assertNotIn("10.0.0.7:5000", node.known_addresses)
        self.assertEqual(node.stats["addresses_out_of_reach"], 2)

    def test_lan_peer_may_send_lan_and_public_addresses(self):
        node = self.connected_from("192.168.1.30")
        actions = node.on_message(1, message(PEERS, addresses=["127.0.0.1:7000", "192.168.1.40:5000", "9.9.9.9:5000"]))
        self.assertEqual(actions, [Connect("192.168.1.40:5000"), Connect("9.9.9.9:5000")])

    def test_same_machine_peer_may_send_anything(self):
        node = self.connected_from("127.0.0.1")
        actions = node.on_message(1, message(PEERS, addresses=["127.0.0.1:7000", "192.168.1.40:5000"]))
        self.assertEqual(actions, [Connect("127.0.0.1:7000"), Connect("192.168.1.40:5000")])

    def test_dialing_is_bounded_by_outbound_connections_only(self):
        node = Node(node_id="me", listen_port=5000, max_peers=2, max_inbound=8)
        for peer_id in (1, 2, 3):  # trois entrées : elles ne comptent pas dans max_peers
            node.on_connect(peer_id, f"10.0.0.{peer_id}", False)
            node.on_message(peer_id, client_hello(f"p{peer_id}", listen_port=6000))
        actions = node.on_message(1, message(PEERS, addresses=["10.0.1.5:5000", "10.0.1.6:5000", "10.0.1.7:5000"]))
        self.assertEqual(actions, [Connect("10.0.1.5:5000"), Connect("10.0.1.6:5000")])  # max_peers sorties, pas une de plus
        self.assertIn("10.0.1.7:5000", node.known_addresses)  # mémorisée quand même


class OwnAddressTests(unittest.TestCase):
    def setUp(self):
        self.node = Node(node_id="me", listen_port=5000)
        self.events = []
        self.node.add_listener(self.events.append)

    def meet_myself_at(self, address, peer_id=99):
        host = address.rsplit(":", 1)[0]
        self.node.on_connect(peer_id, host, True, address)
        only_disconnect(self.node.on_message(peer_id, self.node.hello()), "soi-même")
        self.node.on_disconnect(peer_id)

    def test_self_connection_teaches_own_address_and_forgets_it(self):
        self.node.on_connect(1, "192.168.1.30", False)
        self.node.on_message(1, client_hello("p", listen_port=6000))
        (connect,) = self.node.on_message(1, message(PEERS, addresses=["192.168.1.9:5000"]))  # nous, sans le savoir
        self.assertEqual(connect, Connect("192.168.1.9:5000"))
        self.meet_myself_at("192.168.1.9:5000")
        self.assertEqual(self.node.own_addresses, ("192.168.1.9:5000",))
        self.assertNotIn("192.168.1.9:5000", self.node.known_addresses)
        self.assertIn(AddressForgotten("192.168.1.9:5000"), self.events)
        # Ré-annoncée par un pair : plus jamais rappelée, ni mémorisée.
        self.assertEqual(self.node.on_message(1, message(PEERS, addresses=["192.168.1.9:5000"])), [])
        self.assertNotIn("192.168.1.9:5000", self.node.known_addresses)

    def test_inbound_side_of_a_self_connection_learns_too(self):
        self.node.on_connect(1, "192.168.1.9", False)
        only_disconnect(self.node.on_message(1, self.node.hello()), "soi-même")
        self.assertEqual(self.node.own_addresses, ("192.168.1.9:5000",))

    def test_own_address_is_announced_to_peers_in_reach(self):
        self.meet_myself_at("192.168.1.9:5000")
        self.node.on_connect(1, "192.168.1.30", False)
        self.assertEqual(peers_sent(self.node.on_message(1, client_hello("lan"))), ["192.168.1.9:5000"])
        self.node.on_connect(2, "1.1.1.1", False)
        self.assertIsNone(peers_sent(self.node.on_message(2, client_hello("internet"))))

    def test_redialing_own_address_does_not_refill_the_book(self):
        self.meet_myself_at("192.168.1.9:5000")
        self.node.on_connect(1, "192.168.1.9", True, "192.168.1.9:5000")  # ex. vieux carnet relu au redémarrage
        self.assertNotIn("192.168.1.9:5000", self.node.known_addresses)
        self.assertEqual([e for e in self.events if isinstance(e, AddressLearned)], [AddressLearned("192.168.1.9:5000")])

    def test_own_addresses_are_bounded(self):
        for i in range(node_module.MAX_OWN_ADDRESSES + 3):
            self.meet_myself_at(f"10.0.{i}.1:5000", peer_id=i)
        self.assertEqual(len(self.node.own_addresses), node_module.MAX_OWN_ADDRESSES)

    def test_loopback_with_own_port_is_still_recognised(self):
        self.node.on_connect(1, "127.0.0.1", False)
        self.node.on_message(1, client_hello("p", listen_port=6000))
        self.assertEqual(self.node.on_message(1, message(PEERS, addresses=["127.0.0.2:5000", "localhost:5000"])), [])


class InboundLimitTests(unittest.TestCase):
    def setUp(self):
        self.node = Node(node_id="me", listen_port=5000, max_inbound=2)
        self.node.on_connect(1, "10.0.0.1", False)
        self.node.on_connect(2, "10.0.0.2", False)

    def test_extra_inbound_connection_is_refused_before_hello(self):
        only_disconnect(self.node.on_connect(3, "10.0.0.3", False), "connexions entrantes")
        self.assertEqual(self.node.connections, 2)
        self.assertIsNone(self.node.peer(3))
        self.assertEqual(self.node.stats["inbound_refused"], 1)
        self.assertEqual(self.node.on_message(3, client_hello("late")), [])  # connexion inconnue : rien ne se passe

    def test_outbound_connections_are_counted_apart(self):
        (action,) = self.node.on_connect(3, "10.0.0.3", True, "10.0.0.3:5000")
        self.assertIsInstance(action, Send)
        self.assertEqual((self.node.inbound_connections, self.node.outbound_connections), (2, 1))

    def test_slot_is_freed_on_disconnect(self):
        self.node.on_disconnect(1)
        (action,) = self.node.on_connect(3, "10.0.0.3", False)
        self.assertIsInstance(action, Send)

    def test_simulated_network_refuses_extra_inbound(self):
        net = SimulatedNetwork()
        net.add(Node(node_id="A", max_inbound=1))
        net.add(Node(node_id="B"))
        net.add(Node(node_id="C"))
        net.connect("B", "A")
        net.connect("C", "A")
        self.assertTrue(net.connected("A", "B"))
        self.assertFalse(net.connected("A", "C"))
        self.assertEqual([d for d in net.disconnections if d[0] == "A"], [("A", "C", "plus de 1 connexions entrantes")])
        self.assertEqual(net.node("C").connections, 0)


# ------------------------------------------------------------------ network


class OpenSocketFixture(ServerFixture):
    async def start(self, name, miner=None, *, host="127.0.0.1", hello_timeout=HELLO_TIMEOUT_SECONDS, **node_kwargs):
        node = Node(node_id=name, miner_address=miner, **node_kwargs)
        server = NodeServer(node, host=host, mining_chunk=512, hello_timeout=hello_timeout)
        await server.start()
        self.servers.append(server)
        return server


class HelloTimeoutTests(OpenSocketFixture):
    async def test_silent_connection_is_closed(self):
        a = await self.start("A", hello_timeout=0.3)
        reader, _ = await self.raw_client(a)
        self.assertEqual(decode_message(await asyncio.wait_for(reader.readline(), 5)).type, HELLO)  # le nœud se présente
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 1))
        self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b"")  # ... puis ferme, faute de hello
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0))

    async def test_hello_in_time_keeps_the_connection(self):
        a = await self.start("A", hello_timeout=0.3)
        reader, writer = await self.raw_client(a)
        writer.write(encode_message(client_hello("client")))
        await writer.drain()
        await asyncio.sleep(0.6)
        self.assertEqual(a.node.connections, 1)
        self.assertEqual(a.node.peers[0].node_id, "client")

    async def test_no_timeout_when_disabled(self):
        a = await self.start("A", hello_timeout=None)
        reader, _ = await self.raw_client(a)
        await asyncio.wait_for(reader.readline(), 5)
        await asyncio.sleep(0.3)
        self.assertEqual(a.node.connections, 1)


class InboundLimitOverSocketsTests(OpenSocketFixture):
    async def test_second_inbound_is_closed_without_hello(self):
        a = await self.start("A", max_inbound=1)
        first, _ = await self.raw_client(a)
        self.assertEqual(decode_message(await asyncio.wait_for(first.readline(), 5)).type, HELLO)
        second, _ = await self.raw_client(a)
        self.assertEqual(await asyncio.wait_for(second.readline(), 5), b"")
        self.assertEqual(a.node.connections, 1)
        self.assertEqual(a.node.stats["inbound_refused"], 1)


LAN_IPS = local_ip_addresses()


@unittest.skipUnless(LAN_IPS, "aucune adresse réseau locale : test LAN impossible")
class LanTests(OpenSocketFixture):
    """A écoute sur toutes les interfaces ; on le joint par l'IP réseau de la machine, comme le ferait un autre poste."""

    async def test_public_node_is_reachable_by_lan_ip_and_never_announces_loopback_outside(self):
        lan_ip = LAN_IPS[0]
        a = await self.start("A", host="0.0.0.0")
        d = await self.start("D", host="0.0.0.0")
        c = await self.start("C")  # boucle locale seulement
        self.assertTrue(await d.connect(f"{lan_ip}:{a.port}"))
        self.assertTrue(await c.connect(f"127.0.0.1:{a.port}"))
        self.assertTrue(await a.wait_until(lambda: len(a.node.peers) == 2))
        self.assertIn(f"{lan_ip}:{d.port}", a.node.known_addresses)  # D vu par son IP réseau
        self.assertIn(f"127.0.0.1:{c.port}", a.node.known_addresses)  # C vu par la boucle locale
        b = await self.start("B", host="0.0.0.0")
        self.assertTrue(await b.connect(f"{lan_ip}:{a.port}"))  # B arrive « du réseau local »
        self.assertTrue(await b.wait_until(lambda: {p.node_id for p in b.node.peers} == {"A", "D"}))
        self.assertNotIn(f"127.0.0.1:{c.port}", b.node.known_addresses)  # jamais annoncée hors de la machine
        self.assertNotIn("C", {p.node_id for p in b.node.peers})


# ------------------------------------------------------------------ storage


class StorageForgetTests(unittest.TestCase):
    def test_book_is_rewritten_when_the_node_recognises_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = NodeStorage(Path(tmp) / "a")
            node = storage.open_node(node_id="A", listen_port=5000)
            node.on_connect(1, "192.168.1.9", True, "192.168.1.9:5000")
            self.assertEqual(json.loads(storage.peers_path.read_text(encoding="utf-8"))["addresses"], ["192.168.1.9:5000"])
            node.on_message(1, node.hello())
            self.assertEqual(json.loads(storage.peers_path.read_text(encoding="utf-8"))["addresses"], [])
            self.assertEqual(NodeStorage(storage.directory).load_addresses(), ())


# ---------------------------------------------------------------------- cli


class PublicFlagTests(unittest.TestCase):
    def test_default_listens_on_loopback_only(self):
        args = build_parser().parse_args(["node"])
        self.assertFalse(args.public)
        self.assertEqual(listen_host(args), "127.0.0.1")

    def test_public_listens_on_all_interfaces(self):
        self.assertEqual(listen_host(build_parser().parse_args(["node", "--public"])), "0.0.0.0")

    def test_explicit_host(self):
        self.assertEqual(listen_host(build_parser().parse_args(["node", "--host", "192.168.1.9"])), "192.168.1.9")

    def test_public_and_host_are_exclusive(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["node", "--public", "--host", "192.168.1.9"])


if __name__ == "__main__":
    unittest.main()
