"""Partie 12 : API HTTP du nœud, sur de vraies sockets locales (ports choisis par le système)."""

import asyncio
import json
import unittest

from powchain.api import MAX_BODY_BYTES, ApiServer
from powchain.address import to_checksummed_address
from powchain.codec import transaction_to_dict
from powchain.money import MIN_RELAY_FEE, block_reward
from powchain.network import NodeServer
from powchain.node import Node
from powchain.protocol import PROTOCOL_VERSION
from tests.helpers import ALICE, BOB, MINER, coins, mined, signed_tx

FEE = MIN_RELAY_FEE


async def raw_request(host: str, port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(payload)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(), 5.0)
    finally:
        writer.close()


async def http(api: ApiServer, method: str, path: str, body=None, headers: dict | None = None):
    """Requête HTTP minimale ; retourne (statut, en-têtes, corps décodé si JSON)."""
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    lines = [f"{method} {path} HTTP/1.1", f"Host: {api.host}", f"Content-Length: {len(payload)}"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    raw = await raw_request(api.host, api.port, ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    head, _, content = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    status = int(status_line.split(" ")[1])
    response_headers = {name.lower(): value for name, value in (line.split(": ", 1) for line in header_lines)}
    if content and response_headers.get("content-type", "").startswith("application/json"):
        return status, response_headers, json.loads(content.decode("utf-8"))
    return status, response_headers, content


class ApiFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers: list[NodeServer] = []
        self.apis: list[ApiServer] = []
        self.server = await self.start_node("A")
        self.node = self.server.node
        self.api = ApiServer(self.server, log=lambda text: None)
        await self.api.start()
        self.apis.append(self.api)

    async def asyncTearDown(self):
        for api in self.apis:
            await api.stop()
        for server in self.servers:
            await server.stop()

    async def start_node(self, name: str, **kwargs) -> NodeServer:
        # Pas de miner_address à la construction : le minage automatique ne démarre pas, on mine à la main.
        server = NodeServer(Node(node_id=name, **kwargs), mining_chunk=512)
        await server.start()
        self.servers.append(server)
        return server

    def mine(self, count: int = 1):
        """Mine count blocs vers MINER, à la main, sans boucle de minage."""
        self.node.miner_address = MINER.address
        block = None
        for _ in range(count):
            block = mined(self.node.build_candidate())
            self.node.submit_block(block)
        return block

    async def get(self, path: str):
        return await http(self.api, "GET", path)

    async def post(self, path: str, body):
        return await http(self.api, "POST", path, body, {"Content-Type": "application/json"})


class ReadRoutesTests(ApiFixture):
    async def test_index_and_status(self):
        status, headers, data = await self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "FLOUS")
        self.assertEqual(data["coin_symbol"], "FLS")
        self.assertEqual(data["engine"], "powchain")
        self.assertTrue(any(route.startswith("POST /transactions") for route in data["routes"]))
        self.assertEqual(headers["access-control-allow-origin"], "*")
        self.mine(2)
        status, _, data = await self.get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["height"], 2)
        self.assertEqual(data["tip_hash"], self.node.tip.hash)
        self.assertEqual(data["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(data["min_fee"], MIN_RELAY_FEE)
        self.assertEqual(data["total_supply"], block_reward(1) + block_reward(2))
        self.assertEqual(data["miner_address"], MINER.address)
        self.assertFalse(data["mining"])
        self.assertEqual(data["peers"], 0)

    async def test_blocks_are_paginated_newest_first(self):
        self.mine(5)
        status, _, data = await self.get("/blocks?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual([b["index"] for b in data["blocks"]], [5, 4])
        self.assertEqual(data["next_before"], 4)  # « before » est exclusif : la page suivante commence sous 4
        self.assertEqual(data["blocks"][0]["miner"], MINER.address)
        self.assertEqual(data["blocks"][0]["reward"], block_reward(5))
        status, _, data = await self.get("/blocks?limit=2&before=4")
        self.assertEqual([b["index"] for b in data["blocks"]], [3, 2])
        self.assertEqual(data["next_before"], 2)
        status, _, data = await self.get("/blocks?limit=2&before=2")
        self.assertEqual([b["index"] for b in data["blocks"]], [1, 0])
        self.assertIsNone(data["next_before"])
        status, _, data = await self.get("/blocks?limit=3&before=1")
        self.assertEqual([b["index"] for b in data["blocks"]], [0])
        self.assertIsNone(data["next_before"])
        for bad in ("/blocks?limit=0", "/blocks?limit=abc", "/blocks?before=99", "/blocks?limit=201"):
            status, _, data = await self.get(bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("paramètre", data["error"])

    async def test_block_by_index_or_hash(self):
        block = self.mine(1)
        status, _, by_index = await self.get("/blocks/1")
        self.assertEqual(status, 200)
        self.assertEqual(by_index["hash"], block.hash)
        self.assertEqual(by_index["confirmations"], 1)
        self.assertEqual(by_index["miner"], MINER.address)
        self.assertEqual(len(by_index["transactions"]), 1)
        status, _, by_hash = await self.get(f"/blocks/{block.hash}")
        self.assertEqual(by_hash, by_index)
        self.assertEqual((await self.get("/blocks/99"))[0], 404)
        self.assertEqual((await self.get("/blocks/" + "f" * 64))[0], 404)
        self.assertEqual((await self.get("/blocks/zzz"))[0], 400)

    async def test_transaction_pending_then_confirmed(self):
        self.mine(1)
        tx = signed_tx(MINER, ALICE, coins(1), fee=FEE)
        self.assertEqual((await self.post("/transactions", transaction_to_dict(tx)))[0], 202)
        status, _, data = await self.get(f"/transactions/{tx.hash}")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["confirmations"], 0)
        self.assertEqual(data["fee"], FEE)
        self.mine(1)
        status, _, data = await self.get(f"/transactions/{tx.hash}")
        self.assertEqual(data["status"], "confirmed")
        self.assertEqual(data["block_index"], 2)
        self.assertEqual(data["block_hash"], self.node.tip.hash)
        self.assertEqual(data["confirmations"], 1)
        self.mine(1)
        self.assertEqual((await self.get(f"/transactions/{tx.hash}"))[2]["confirmations"], 2)
        self.assertEqual((await self.get("/transactions/" + "0" * 64))[0], 404)
        self.assertEqual((await self.get("/transactions/nope"))[0], 400)

    async def test_account_and_history(self):
        self.mine(1)
        checksummed = to_checksummed_address(MINER.address)
        status, _, data = await self.get(f"/accounts/{MINER.address}")
        self.assertEqual(status, 200)
        self.assertEqual(data["balance"], block_reward(1))
        self.assertEqual(data["checksummed"], checksummed)
        self.assertEqual(data["transactions"], 1)  # la coinbase
        self.assertEqual((await self.get(f"/accounts/{checksummed}"))[2], data)
        self.assertEqual((await self.get("/accounts/abc"))[0], 400)

        tx = signed_tx(MINER, ALICE, coins(10), fee=FEE)
        await self.post("/transactions", transaction_to_dict(tx))
        _, _, data = await self.get(f"/accounts/{MINER.address}")
        self.assertEqual(data["balance"], block_reward(1))
        self.assertEqual(data["projected_balance"], block_reward(1) - coins(10) - FEE)
        self.assertEqual(data["projected_next_sequence"], 1)
        self.assertEqual(data["pending"], 1)
        _, _, history = await self.get(f"/accounts/{ALICE.address}/transactions")
        self.assertEqual(history["total"], 0)
        self.assertEqual([t["hash"] for t in history["pending"]], [tx.hash])

        self.mine(1)
        _, _, history = await self.get(f"/accounts/{ALICE.address}/transactions")
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["pending"], [])
        self.assertEqual(history["transactions"][0]["hash"], tx.hash)
        self.assertEqual(history["transactions"][0]["block_index"], 2)
        _, _, history = await self.get(f"/accounts/{MINER.address}/transactions?limit=1")
        self.assertEqual(history["total"], 3)  # 2 coinbases + le paiement
        self.assertEqual(history["transactions"][0]["block_index"], 2)  # plus récent d'abord
        _, _, history = await self.get(f"/accounts/{MINER.address}/transactions?limit=1&offset=2")
        self.assertEqual(history["transactions"][0]["block_index"], 1)

    async def test_mempool_lists_best_payers_first(self):
        self.mine(1)
        cheap = signed_tx(MINER, ALICE, coins(1), fee=FEE, sequence=0)
        dear = signed_tx(MINER, BOB, coins(1), fee=3 * FEE, sequence=1)
        await self.post("/transactions", transaction_to_dict(cheap))
        await self.post("/transactions", transaction_to_dict(dear))
        status, _, data = await self.get("/mempool")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total_fees"], 4 * FEE)
        self.assertEqual([t["hash"] for t in data["transactions"]], [dear.hash, cheap.hash])


class WriteRoutesTests(ApiFixture):
    async def test_post_transaction_is_accepted_and_gossiped(self):
        self.mine(1)
        other = await self.start_node("B")
        await other.connect(self.server.address)
        self.assertTrue(await other.wait_until(lambda: len(other.node.peers) == 1, timeout=5))
        tx = signed_tx(MINER, ALICE, coins(1), fee=FEE)
        status, _, data = await self.post("/transactions", transaction_to_dict(tx))
        self.assertEqual(status, 202)
        self.assertEqual(data["hash"], tx.hash)
        self.assertTrue(data["accepted"])
        self.assertEqual(data["projected_balance"], block_reward(1) - coins(1) - FEE)
        self.assertEqual(data["projected_next_sequence"], 1)
        self.assertIn(tx, self.node.mempool)
        self.assertTrue(await other.wait_until(lambda: tx in other.node.mempool, timeout=5))
        # Re-soumettre une transaction déjà connue n'est pas une erreur.
        self.assertEqual((await self.post("/transactions", transaction_to_dict(tx)))[0], 202)
        status, _, data = await self.get("/peers")
        self.assertEqual([p["node_id"] for p in data["peers"]], ["B"])
        self.assertFalse(data["peers"][0]["outbound"])

    async def test_post_transaction_errors(self):
        self.mine(1)
        status, _, data = await http(self.api, "POST", "/transactions", None)
        self.assertEqual(status, 400)
        self.assertIn("JSON", data["error"])
        status, _, data = await self.post("/transactions", ["pas", "un", "objet"])
        self.assertEqual(status, 400)
        incomplete = transaction_to_dict(signed_tx(MINER, ALICE, coins(1), fee=FEE))
        del incomplete["signature"]
        status, _, data = await self.post("/transactions", incomplete)
        self.assertEqual(status, 400)
        self.assertIn("manquant", data["error"])
        cheap = signed_tx(MINER, ALICE, coins(1), fee=0)
        status, _, data = await self.post("/transactions", transaction_to_dict(cheap))
        self.assertEqual(status, 400)
        self.assertIn("frais insuffisants", data["error"])
        self.assertEqual(data["hash"], cheap.hash)
        broke = signed_tx(ALICE, BOB, coins(1), fee=FEE)
        status, _, data = await self.post("/transactions", transaction_to_dict(broke))
        self.assertEqual(status, 400)
        self.assertIn("solde insuffisant", data["error"])
        self.assertEqual(len(self.node.mempool), 0)


class HttpEdgeCaseTests(ApiFixture):
    async def test_cors_preflight_methods_and_unknown_routes(self):
        status, headers, body = await http(self.api, "OPTIONS", "/transactions")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertIn("POST", headers["access-control-allow-methods"])
        self.assertEqual((await http(self.api, "PUT", "/status"))[0], 405)
        self.assertEqual((await self.post("/status", {}))[0], 405)
        self.assertEqual((await self.get("/transactions"))[0], 405)
        self.assertEqual((await self.get("/nope"))[0], 404)
        self.assertEqual((await self.get("/blocks/1/extra"))[0], 404)
        status, headers, body = await http(self.api, "HEAD", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["content-length"]), 0)

    async def test_malformed_and_oversized_requests(self):
        raw = await raw_request(self.api.host, self.api.port, b"GARBAGE\r\n\r\n")
        self.assertTrue(raw.startswith(b"HTTP/1.1 400"))
        raw = await raw_request(self.api.host, self.api.port, b"GET /status HTTP/1.1\r\nSansDeuxPoints\r\n\r\n")
        self.assertTrue(raw.startswith(b"HTTP/1.1 400"))
        raw = await raw_request(
            self.api.host, self.api.port, f"POST /transactions HTTP/1.1\r\nContent-Length: {MAX_BODY_BYTES + 1}\r\n\r\n".encode()
        )
        self.assertTrue(raw.startswith(b"HTTP/1.1 413"))
        raw = await raw_request(self.api.host, self.api.port, b"GET /status HTTP/1.1\r\nContent-Length: abc\r\n\r\n")
        self.assertTrue(raw.startswith(b"HTTP/1.1 400"))
        # Le nœud continue de répondre normalement après ces requêtes.
        self.assertEqual((await self.get("/status"))[0], 200)


class ExplorerTests(ApiFixture):
    async def test_root_serves_html_to_browsers_and_json_to_api_clients(self):
        # Un navigateur (Accept: text/html) reçoit l'explorateur de blocs.
        status, headers, body = await http(self.api, "GET", "/", headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn(b"FLOUS", body)
        self.assertIn(b"<canvas", body)
        # Sans Accept HTML, la racine reste l'index JSON (clients API, curl, tests).
        status, headers, data = await self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "FLOUS")

    async def test_explorer_route_always_html(self):
        status, headers, body = await http(self.api, "GET", "/explorer")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn(b"FLS", body)
        self.assertEqual((await self.post("/explorer", {}))[0], 405)


if __name__ == "__main__":
    unittest.main()
