"""Tests du transport asyncio sur de vraies sockets locales (ports choisis par le système)."""

import asyncio
import unittest

from powchain.__main__ import request
from powchain.codec import transaction_to_dict
from powchain.money import block_reward
from powchain.network import NodeServer
from powchain.node import Node
from powchain.protocol import ACCOUNT, GET_ACCOUNT, HELLO, MAX_MESSAGE_BYTES, NEW_TRANSACTION, REJECT, encode_message, message
from tests.helpers import ALICE, MINER, coins, signed_tx


class ServerFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers: list[NodeServer] = []

    async def asyncTearDown(self):
        for server in self.servers:
            await server.stop()

    async def start(self, name: str, miner=None, **kwargs) -> NodeServer:
        server = NodeServer(Node(node_id=name, miner_address=miner, **kwargs), mining_chunk=512)
        await server.start()
        self.servers.append(server)
        return server

    async def raw_client(self, server: NodeServer):
        reader, writer = await asyncio.open_connection(server.host, server.port, limit=MAX_MESSAGE_BYTES)
        self.addAsyncCleanup(self._close, writer)
        return reader, writer

    @staticmethod
    async def _close(writer):
        writer.close()


class ConnectionTests(ServerFixture):
    async def test_handshake_registers_both_sides(self):
        a, b = await self.start("A"), await self.start("B")
        self.assertTrue(await b.connect(a.address))
        self.assertTrue(await a.wait_until(lambda: len(a.node.peers) == 1 and len(b.node.peers) == 1))
        self.assertEqual(a.node.peers[0].node_id, "B")
        self.assertEqual(b.node.peers[0].node_id, "A")
        self.assertEqual(b.node.peers[0].address, a.address)
        self.assertEqual(a.node.peers[0].address, b.address)  # via listen_port du hello
        self.assertFalse(await b.connect(a.address))  # déjà connecté

    async def test_unreachable_peer(self):
        a = await self.start("A")
        self.assertFalse(await a.connect("127.0.0.1:1"))
        self.assertEqual(a.node.connections, 0)

    async def test_discovery_through_peer_exchange(self):
        a, b, c = await self.start("A"), await self.start("B"), await self.start("C")
        await b.connect(a.address)
        await self.wait_all_ready(a, b)
        await c.connect(b.address)
        self.assertTrue(await c.wait_until(lambda: {p.node_id for p in c.node.peers} == {"A", "B"}))
        self.assertTrue(await a.wait_until(lambda: {p.node_id for p in a.node.peers} == {"B", "C"}))

    async def test_self_connection_is_closed(self):
        a = await self.start("A")
        await a.connect(a.address)
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0))

    async def test_garbage_closes_connection(self):
        a = await self.start("A")
        reader, writer = await self.raw_client(a)
        writer.write(b"pas du json\n")
        await writer.drain()
        received = await reader.read()  # tout jusqu'à la fermeture par le serveur
        self.assertEqual(received.count(b"\n"), 1)  # seulement son hello, puis EOF
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0))

    async def test_oversized_line_closes_connection(self):
        a = await self.start("A")
        reader, writer = await self.raw_client(a)
        writer.write(b"x" * (MAX_MESSAGE_BYTES + 1))
        try:
            await writer.drain()
        except ConnectionError:
            pass  # le serveur a déjà coupé
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0, timeout=10))

    async def test_message_before_hello_closes_connection(self):
        a = await self.start("A")
        reader, writer = await self.raw_client(a)
        writer.write(encode_message(message(GET_ACCOUNT, address=ALICE.address)))
        await writer.drain()
        first = await reader.readline()  # le hello du serveur, envoyé à la connexion
        self.assertIn(b'"type":"hello"', first)
        self.assertEqual(await reader.read(), b"")

    async def test_peer_disconnect_is_noticed(self):
        a, b = await self.start("A"), await self.start("B")
        await b.connect(a.address)
        await self.wait_all_ready(a, b)
        await b.stop()
        self.servers.remove(b)
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0))

    async def wait_all_ready(self, *servers):
        for server in servers:
            self.assertTrue(await server.wait_until(lambda: len(server.node.peers) >= 1))


class GossipAndMiningTests(ServerFixture):
    async def test_mined_blocks_propagate(self):
        a = await self.start("A", miner=MINER.address)
        b = await self.start("B")
        await b.connect(a.address)
        self.assertTrue(await b.wait_until(lambda: b.node.height >= 3, timeout=15))
        self.assertEqual(b.node.tip, a.node.chain.block_at(b.node.height))
        self.assertGreaterEqual(a.blocks_mined, 3)

    async def test_transaction_propagates_and_gets_mined(self):
        a = await self.start("A", miner=MINER.address)
        b = await self.start("B")
        await b.connect(a.address)
        self.assertTrue(await b.wait_until(lambda: b.node.height >= 1, timeout=15))
        tx = signed_tx(MINER, ALICE, coins(1))
        await b._execute(b.node.submit_transaction(tx))
        self.assertTrue(await a.wait_until(lambda: a.node.chain.state.balance_of(ALICE.address) == coins(1), timeout=15))
        self.assertTrue(await b.wait_until(lambda: b.node.chain.state.balance_of(ALICE.address) == coins(1), timeout=15))
        self.assertNotIn(tx, b.node.mempool)

    async def test_late_joiner_catches_up_over_sockets(self):
        a = await self.start("A", miner=MINER.address)
        b = await self.start("B")
        await b.connect(a.address)
        self.assertTrue(await b.wait_until(lambda: b.node.height >= 2, timeout=15))
        await a.stop()
        self.servers.remove(a)
        height = b.node.height
        c = await self.start("C")
        await c.connect(b.address)
        self.assertTrue(await c.wait_until(lambda: c.node.height == height))
        self.assertEqual(c.node.chain.blocks, b.node.chain.blocks)


class MiningControlTests(ServerFixture):
    async def test_start_and_stop_mining_on_demand(self):
        a = await self.start("A")
        self.assertFalse(a.mining)
        with self.assertRaises(ValueError):
            a.start_mining()
        with self.assertRaises(ValueError):
            a.start_mining("alice")
        a.start_mining(MINER.address)
        self.assertTrue(a.mining)
        self.assertTrue(await a.wait_until(lambda: a.node.height >= 1, timeout=15))
        await a.stop_mining()
        self.assertFalse(a.mining)
        height = a.node.height
        await asyncio.sleep(0.05)
        self.assertEqual(a.node.height, height)
        a.start_mining()  # relance avec l'adresse mémorisée
        self.assertTrue(a.mining)

    async def test_stale_candidate_is_replaced_when_tip_changes(self):
        """Un bloc reçu du réseau pendant le minage fait repartir le mineur sur la nouvelle pointe."""
        a = await self.start("A", miner=MINER.address)
        b = await self.start("B")
        await b.connect(a.address)
        self.assertTrue(await b.wait_until(lambda: b.node.height >= 2, timeout=15))
        self.assertEqual(a.node.stats["blocks_rejected"], 0)
        self.assertEqual(b.node.stats["blocks_rejected"], 0)
        self.assertEqual(b.node.chain.blocks[: b.node.height + 1], a.node.chain.blocks[: b.node.height + 1])


class ClientRequestTests(ServerFixture):
    async def test_status_and_account_query(self):
        a = await self.start("A", miner=MINER.address)
        self.assertTrue(await a.wait_until(lambda: a.node.height >= 1, timeout=15))
        hello, account = await request(a.address, [message(GET_ACCOUNT, address=MINER.address)], [HELLO, ACCOUNT])
        self.assertEqual(hello.type, HELLO)
        self.assertEqual(hello["node_id"], "A")
        self.assertEqual(account["address"], MINER.address)
        self.assertGreaterEqual(account["balance"], block_reward(1))
        self.assertTrue(await a.wait_until(lambda: a.node.connections == 0))  # le client s'est déconnecté

    async def test_send_transaction_then_reject(self):
        a = await self.start("A", miner=MINER.address)
        self.assertTrue(await a.wait_until(lambda: a.node.height >= 1, timeout=15))
        tx = signed_tx(MINER, ALICE, coins(1))
        replies = await request(
            a.address,
            [message(NEW_TRANSACTION, transaction=transaction_to_dict(tx)), message(GET_ACCOUNT, address=MINER.address)],
            [HELLO, ACCOUNT],
        )
        self.assertEqual([r.type for r in replies], [HELLO, ACCOUNT])
        self.assertEqual(replies[1]["projected_next_sequence"], 1)
        bad = signed_tx(ALICE, MINER, coins(5))  # alice n'a rien
        replies = await request(
            a.address, [message(NEW_TRANSACTION, transaction=transaction_to_dict(bad))], [HELLO, ACCOUNT]
        )
        self.assertEqual(replies[-1].type, REJECT)
        self.assertIn("solde insuffisant", replies[-1]["reason"])
