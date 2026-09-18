import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block
from powchain.chain import Blockchain
from powchain.codec import block_to_dict, transaction_to_dict
from powchain.errors import StorageError
from powchain.node import Node
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.protocol import NEW_BLOCK, message
from powchain.simulation import FakeClock, SimulatedNetwork
from powchain.storage import BLOCKS_FILE, MEMPOOL_FILE, PEERS_FILE, NodeStorage
from powchain.transaction import create_transaction
from tests.helpers import ALICE, BOB, MALLORY, MINER, coins, mined, signed_tx

START = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock(START)

    def directory(self, name="a") -> Path:
        return self.root / name

    def network_with_storage(self, name="A", miner=MINER, directory=None):
        """Nœud suivi par un stockage, branché sur un réseau simulé (retourne net, node, storage)."""
        storage = NodeStorage(directory or self.directory(name.lower()))
        node = storage.open_node(node_id=name, miner_address=miner.address if miner else None, clock=self.clock)
        net = SimulatedNetwork(self.clock)
        net.add(node)
        return net, node, storage

    def mine(self, net, name, count=1):
        for _ in range(count):
            self.clock.advance(TARGET_BLOCK_TIME)
            net.mine(name)


class FreshDirectoryTests(StorageFixture):
    def test_open_node_creates_files_with_genesis(self):
        storage = NodeStorage(self.directory())
        self.assertFalse(storage.exists())
        node = storage.open_node(node_id="A")
        self.assertEqual(node.height, 0)
        self.assertTrue(storage.exists())
        self.assertEqual(read_lines(self.directory() / BLOCKS_FILE), [json.dumps(block_to_dict(create_genesis_block()), separators=(",", ":"))])
        self.assertEqual((self.directory() / MEMPOOL_FILE).read_text(encoding="utf-8"), "")
        self.assertEqual(json.loads((self.directory() / PEERS_FILE).read_text(encoding="utf-8")), {"version": 1, "addresses": []})
        self.assertFalse(list(self.directory().glob("*.tmp")))

    def test_nested_directory_is_created(self):
        storage = NodeStorage(self.root / "deep" / "er" / "node")
        storage.open_node(node_id="A")
        self.assertTrue((self.root / "deep" / "er" / "node" / BLOCKS_FILE).exists())

    def test_empty_blocks_file_means_genesis_and_is_rewritten(self):
        self.directory().mkdir()
        (self.directory() / BLOCKS_FILE).write_bytes(b"")
        storage = NodeStorage(self.directory())
        node = storage.open_node(node_id="A")
        self.assertEqual(node.height, 0)
        self.assertEqual(len(read_lines(self.directory() / BLOCKS_FILE)), 1)

    def test_follow_writes_a_chain_the_disk_does_not_have(self):
        chain = Blockchain()
        chain.add_block(mined(create_block(chain.last_block, [], MINER.address, timestamp=START)))
        node = Node(node_id="A", chain=chain)
        storage = NodeStorage(self.directory())
        storage.follow(node)
        self.assertEqual(NodeStorage(self.directory()).load_chain().blocks, chain.blocks)


class RoundTripTests(StorageFixture):
    def test_blocks_are_appended_and_reloaded(self):
        net, node, storage = self.network_with_storage()
        self.mine(net, "A", 3)
        self.assertEqual(len(read_lines(storage.blocks_path)), 4)
        reloaded = NodeStorage(storage.directory).open_node(node_id="A2")
        self.assertEqual(reloaded.chain.blocks, node.chain.blocks)
        self.assertEqual(reloaded.chain.state, node.chain.state)
        self.assertEqual(reloaded.height, 3)

    def test_mempool_survives_restart(self):
        net, node, storage = self.network_with_storage()
        self.mine(net, "A")
        pending = signed_tx(MINER, ALICE, coins(1))
        net.run("A", node.submit_transaction(pending))
        self.assertEqual(len(read_lines(storage.mempool_path)), 1)
        reloaded = NodeStorage(storage.directory).open_node(node_id="A2")
        self.assertEqual(reloaded.mempool.transactions, (pending,))
        # Le bloc suivant la confirme : le fichier du mempool est vidé.
        net.add(reloaded)
        self.clock.advance(TARGET_BLOCK_TIME)
        reloaded.miner_address = MINER.address
        net.mine("A2")
        self.assertEqual((storage.directory / MEMPOOL_FILE).read_text(encoding="utf-8"), "")

    def test_stale_mempool_transaction_is_dropped_at_load(self):
        net, node, storage = self.network_with_storage()
        self.mine(net, "A")
        pending = signed_tx(MINER, ALICE, coins(1), sequence=0)
        net.run("A", node.submit_transaction(pending))
        # Pendant que le nœud est éteint, la même séquence est confirmée ailleurs : le fichier de blocs le sait.
        elsewhere = signed_tx(MINER, BOB, coins(2), sequence=0)
        self.clock.advance(TARGET_BLOCK_TIME)
        block = mined(create_block(node.tip, [elsewhere], MALLORY.address, timestamp=self.clock.now))
        storage.append_block(block)
        fresh = NodeStorage(storage.directory)
        reloaded = fresh.open_node(node_id="A2")
        self.assertEqual(reloaded.height, 2)
        self.assertEqual(len(reloaded.mempool), 0)
        self.assertEqual(fresh.dropped_transactions, 1)
        self.assertEqual((storage.directory / MEMPOOL_FILE).read_text(encoding="utf-8"), "")

    def test_addresses_survive_restart_and_prefill_the_book(self):
        net, node, storage = self.network_with_storage()
        other = net.add(Node(node_id="B", clock=self.clock))
        net.connect("A", "B")
        self.assertEqual(json.loads(storage.peers_path.read_text(encoding="utf-8"))["addresses"], [net.address_of("B")])
        reloaded = NodeStorage(storage.directory).open_node(node_id="A2")
        self.assertEqual(reloaded.known_addresses, (net.address_of("B"),))
        self.assertIsNotNone(other)

    def test_reorganization_rewrites_the_file_atomically(self):
        net, a, storage = self.network_with_storage()
        b = net.add(Node(node_id="B", miner_address=MALLORY.address, clock=self.clock))
        net.connect("A", "B")
        self.mine(net, "A")
        net.partition("A", "B")
        self.mine(net, "A")
        self.mine(net, "B")
        self.clock.advance(1)
        net.mine("B")  # B : 2 blocs contre 1 sur la branche de A
        net.heal("A", "B")
        abandoned = a.tip
        # B annonce sa pointe à A : branche plus lourde, A se réorganise.
        net.run("A", a.on_message(net.peer_id("A", "B"), message(NEW_BLOCK, block=block_to_dict(b.tip))))
        self.assertEqual(a.stats["reorganizations"], 1)
        self.assertNotIn(abandoned, a.chain)
        reloaded = NodeStorage(storage.directory).load_chain()
        self.assertEqual(reloaded.blocks, a.chain.blocks)
        self.assertFalse(list(storage.directory.glob("*.tmp")))

    def test_deleted_file_restarts_from_genesis_and_network_heals(self):
        net, a, storage = self.network_with_storage()
        b = net.add(Node(node_id="B", clock=self.clock))
        net.connect("A", "B")
        self.mine(net, "A", 3)
        storage.blocks_path.unlink()
        fresh = NodeStorage(storage.directory)
        restarted = fresh.open_node(node_id="A2", clock=self.clock)
        self.assertEqual(restarted.height, 0)
        net.add(restarted)
        net.connect("A2", "B")
        self.assertEqual(restarted.height, 3)
        self.assertEqual(len(read_lines(fresh.blocks_path)), 4)
        self.assertEqual(b.height, 3)


class RepairTests(StorageFixture):
    def prepare(self, count=3):
        net, node, storage = self.network_with_storage()
        self.mine(net, "A", count)
        return node, storage

    def test_truncated_last_line_is_dropped_and_file_repaired(self):
        node, storage = self.prepare()
        raw = storage.blocks_path.read_bytes()
        storage.blocks_path.write_bytes(raw[: len(raw) - 40])  # coupé en pleine ligne, sans « \n »
        fresh = NodeStorage(storage.directory)
        reloaded = fresh.open_node(node_id="A2")
        self.assertEqual(reloaded.height, 2)
        self.assertEqual(fresh.repaired_lines, 1)
        self.assertEqual(len(read_lines(fresh.blocks_path)), 3)
        self.assertTrue(storage.blocks_path.read_bytes().endswith(b"\n"))
        self.assertEqual(NodeStorage(storage.directory).open_node(node_id="A3").height, 2)

    def test_complete_last_line_without_newline_is_kept(self):
        node, storage = self.prepare()
        raw = storage.blocks_path.read_bytes()
        storage.blocks_path.write_bytes(raw.rstrip(b"\n"))
        fresh = NodeStorage(storage.directory)
        self.assertEqual(fresh.open_node(node_id="A2").height, 3)
        self.assertEqual(fresh.repaired_lines, 1)
        self.assertTrue(storage.blocks_path.read_bytes().endswith(b"\n"))

    def test_unreadable_last_line_is_dropped(self):
        node, storage = self.prepare()
        with open(storage.blocks_path, "ab") as handle:
            handle.write(b'{"index": 4, "garbage\n')
        fresh = NodeStorage(storage.directory)
        self.assertEqual(fresh.open_node(node_id="A2").height, 3)
        self.assertEqual(fresh.repaired_lines, 1)

    def test_unreadable_middle_line_is_corruption(self):
        node, storage = self.prepare()
        lines = storage.blocks_path.read_bytes().split(b"\n")
        lines[1] = b"not json"
        storage.blocks_path.write_bytes(b"\n".join(lines))
        with self.assertRaisesRegex(StorageError, "ligne 2 : JSON illisible"):
            NodeStorage(storage.directory).load_chain()

    def test_blank_lines_are_ignored(self):
        node, storage = self.prepare()
        storage.blocks_path.write_bytes(storage.blocks_path.read_bytes().replace(b"\n", b"\n\n"))
        self.assertEqual(NodeStorage(storage.directory).load_chain().height, 3)


class TamperingTests(StorageFixture):
    def prepare(self):
        net, node, storage = self.network_with_storage()
        self.mine(net, "A")
        net.run("A", node.submit_transaction(signed_tx(MINER, ALICE, coins(10))))
        self.mine(net, "A", 2)
        return node, storage

    def rewrite_block(self, storage, index, block):
        lines = storage.blocks_path.read_bytes().split(b"\n")
        lines[index] = json.dumps(block_to_dict(block), separators=(",", ":")).encode("utf-8")
        storage.blocks_path.write_bytes(b"\n".join(lines))

    def test_modified_amount_is_detected(self):
        node, storage = self.prepare()
        block2 = node.chain.block_at(2)
        coinbase, payment = block2.transactions
        self.rewrite_block(storage, 2, replace(block2, transactions=(coinbase, replace(payment, amount=coins(20)))))
        with self.assertRaisesRegex(StorageError, "chaîne invalide .*position 2 .*hash incohérent"):
            NodeStorage(storage.directory).load_chain()

    def test_remined_forgery_without_signature_is_detected(self):
        node, storage = self.prepare()
        block2 = node.chain.block_at(2)
        coinbase, payment = block2.transactions
        forged = create_transaction(payment.sender, payment.recipient, coins(20), payment.data, payment.sequence)
        self.rewrite_block(storage, 2, mined(replace(block2, transactions=(coinbase, forged))))
        with self.assertRaisesRegex(StorageError, "non signée"):
            NodeStorage(storage.directory).load_chain()

    def test_inflated_reward_is_detected(self):
        node, storage = self.prepare()
        block1 = node.chain.block_at(1)
        greedy = replace(block1.coinbase, amount=block1.coinbase.amount + 1)
        greedy = replace(greedy, hash=greedy.calculate_hash())
        self.rewrite_block(storage, 1, mined(replace(block1, transactions=(greedy,))))
        with self.assertRaisesRegex(StorageError, "récompense"):
            NodeStorage(storage.directory).load_chain()

    def test_unknown_field_is_rejected(self):
        node, storage = self.prepare()
        lines = storage.blocks_path.read_bytes().split(b"\n")
        data = json.loads(lines[1])
        data["extra"] = 1
        lines[1] = json.dumps(data).encode("utf-8")
        storage.blocks_path.write_bytes(b"\n".join(lines))
        with self.assertRaisesRegex(StorageError, "ligne 2 .*inconnu"):
            NodeStorage(storage.directory).load_chain()

    def test_foreign_but_valid_chain_is_accepted_locally(self):
        """Limite : le disque prouve l'intégrité, pas l'authenticité. Seul le réseau tranche."""
        node, storage = self.prepare()
        other = Blockchain()
        other.add_block(mined(create_block(other.last_block, [], MALLORY.address, timestamp=START)))
        storage.write_blocks(other.blocks)
        reloaded = NodeStorage(storage.directory).load_chain()
        self.assertEqual(reloaded.blocks, other.blocks)
        self.assertNotEqual(reloaded.blocks, node.chain.blocks)

    def test_bad_mempool_line_is_rejected(self):
        node, storage = self.prepare()
        storage.mempool_path.write_text('{"sender": 1}\n', encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "mempool.jsonl, ligne 1"):
            NodeStorage(storage.directory).load_mempool(node.chain.state)

    def test_bad_peers_file_is_rejected_and_bad_entries_skipped(self):
        node, storage = self.prepare()
        storage.peers_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "format inattendu"):
            NodeStorage(storage.directory).load_addresses()
        storage.peers_path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "illisible"):
            NodeStorage(storage.directory).load_addresses()
        storage.peers_path.write_text(json.dumps({"version": 1, "addresses": ["h:1", "pas une adresse", 5]}), encoding="utf-8")
        self.assertEqual(NodeStorage(storage.directory).load_addresses(), ("h:1",))


class WriteFailureTests(StorageFixture):
    def test_unwritable_directory_raises_storage_error(self):
        blocker = self.root / "file.txt"
        blocker.write_text("x", encoding="utf-8")
        storage = NodeStorage(blocker / "sub")  # un fichier à la place du dossier parent
        with self.assertRaises(StorageError):
            storage.open_node(node_id="A")
        node = Node(node_id="B")
        node.chain.add_block(mined(create_block(node.chain.last_block, [], MINER.address, timestamp=START)))
        with self.assertRaises(StorageError):
            storage.append_block(node.tip)
        with self.assertRaises(StorageError):
            storage.write_addresses(["h:1"])

    def test_listener_error_interrupts_the_node(self):
        net, node, storage = self.network_with_storage()
        storage.blocks_path.unlink()
        storage.blocks_path.mkdir()  # un dossier à la place du fichier : l'ajout échoue
        with self.assertRaises(StorageError):
            self.mine(net, "A")
