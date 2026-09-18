import unittest
from dataclasses import replace
from unittest import mock

import powchain.node as node_module
from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block
from powchain.chain import Blockchain, chain_work
from powchain.codec import block_to_dict, blocks_to_list, transaction_to_dict
from powchain.errors import InvalidBlockError, InvalidTransactionError
from powchain.mempool import Mempool
from powchain.node import MAX_FUTURE_DRIFT_SECONDS, Connect, Disconnect, Node, Send
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.protocol import (
    ACCOUNT,
    BLOCKS,
    GET_ACCOUNT,
    GET_BLOCKS,
    HELLO,
    NEW_BLOCK,
    NEW_TRANSACTION,
    PEERS,
    PROTOCOL_VERSION,
    REJECT,
    Message,
    message,
)
from powchain.simulation import FakeClock, SimulatedNetwork
from tests.helpers import ALICE, BOB, CAROL, MALLORY, MINER, coins, mined, signed_tx

START = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME


def hello_from(node: Node, **overrides) -> Message:
    payload = dict(node.hello().payload)
    payload.update(overrides)
    return Message(HELLO, payload)


def sends_of(actions, type_=None):
    return [a for a in actions if isinstance(a, Send) and (type_ is None or a.message.type == type_)]


def only_disconnect(actions, pattern: str):
    (action,) = actions
    assert isinstance(action, Disconnect), actions
    assert pattern in action.reason, action.reason
    return action


def chain_with_blocks(count: int, clock: FakeClock, miner=MINER, spacing: int = TARGET_BLOCK_TIME) -> Blockchain:
    """Chaîne de `count` blocs coinbase seule, espacés de `spacing` secondes ; avance l'horloge."""
    chain = Blockchain()
    for _ in range(count):
        clock.advance(spacing)
        chain.add_block(mined(create_block(chain.last_block, [], miner.address, timestamp=clock.now)))
    return chain


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.node = Node(node_id="me", listen_port=5000, clock=self.clock)
        self.other = Node(node_id="other", listen_port=5001, clock=self.clock)

    def test_on_connect_sends_hello(self):
        (action,) = self.node.on_connect(1, "10.0.0.2", outbound=False)
        self.assertEqual(action, Send(1, self.node.hello()))
        hello = action.message
        self.assertEqual(hello["node_id"], "me")
        self.assertEqual(hello["version"], PROTOCOL_VERSION)
        self.assertEqual(hello["listen_port"], 5000)
        self.assertEqual(hello["height"], 0)
        self.assertEqual(hello["work"], create_genesis_block().difficulty)
        self.assertEqual(hello["tip_hash"], create_genesis_block().hash)
        self.assertEqual(self.node.connections, 1)
        self.assertEqual(self.node.peers, ())

    def test_duplicate_peer_id_is_a_bug(self):
        self.node.on_connect(1, "h", False)
        with self.assertRaises(ValueError):
            self.node.on_connect(1, "h", False)

    def test_hello_registers_peer_and_shares_addresses(self):
        self.node.on_connect(1, "10.0.0.9", False, "10.0.0.9:7000")  # un pair déjà connu
        self.node.on_connect(2, "10.0.0.2", False)
        actions = self.node.on_message(2, self.other.hello())
        peer = self.node.peer(2)
        self.assertTrue(peer.ready)
        self.assertEqual(peer.node_id, "other")
        self.assertEqual(peer.address, "10.0.0.2:5001")
        self.assertEqual(self.node.peers, (peer,))
        (peers_msg,) = sends_of(actions, PEERS)
        self.assertEqual(peers_msg.message["addresses"], ["10.0.0.9:7000"])  # pas sa propre adresse
        self.assertIn("10.0.0.2:5001", self.node.known_addresses)

    def test_no_peers_message_when_nothing_to_share(self):
        self.node.on_connect(1, "h", False)
        actions = self.node.on_message(1, hello_from(self.other, listen_port=None))
        self.assertEqual(actions, [])
        self.assertIsNone(self.node.peer(1).address)

    def test_message_before_hello_disconnects(self):
        self.node.on_connect(1, "h", False)
        only_disconnect(self.node.on_message(1, message(GET_BLOCKS, from_index=0)), "avant hello")

    def test_second_hello_disconnects(self):
        self.node.on_connect(1, "h", False)
        self.node.on_message(1, self.other.hello())
        only_disconnect(self.node.on_message(1, self.other.hello()), "deux fois")

    def test_self_connection_disconnects(self):
        self.node.on_connect(1, "h", True, "127.0.0.1:5000")
        only_disconnect(self.node.on_message(1, self.node.hello()), "soi-même")

    def test_duplicate_node_disconnects(self):
        self.node.on_connect(1, "h", False)
        self.node.on_message(1, self.other.hello())
        self.node.on_connect(2, "h", True)
        only_disconnect(self.node.on_message(2, self.other.hello()), "déjà connecté")
        self.assertEqual(len(self.node.peers), 1)

    def test_wrong_version_disconnects(self):
        self.node.on_connect(1, "h", False)
        only_disconnect(self.node.on_message(1, hello_from(self.other, version=PROTOCOL_VERSION + 1)), "version")

    def test_bad_listen_port_disconnects(self):
        self.node.on_connect(1, "h", False)
        only_disconnect(self.node.on_message(1, hello_from(self.other, listen_port=70000)), "port")

    def test_malformed_message_disconnects(self):
        self.node.on_connect(1, "h", False)
        only_disconnect(self.node.on_message(1, Message("dance", {})), "hors protocole")
        self.node.on_connect(2, "h2", False)  # « h » vient d'être banni (Partie 10) : un autre hôte
        self.node.on_message(2, self.other.hello())
        only_disconnect(self.node.on_message(2, message(NEW_BLOCK, block={"index": 1})), "mal formé")

    def test_unknown_connection_is_ignored(self):
        self.assertEqual(self.node.on_message(42, self.other.hello()), [])
        self.assertEqual(self.node.on_disconnect(42), [])

    def test_disconnect_forgets_peer(self):
        self.node.on_connect(1, "h", False)
        self.node.on_message(1, self.other.hello())
        self.node.on_disconnect(1)
        self.assertEqual(self.node.peers, ())
        self.assertEqual(self.node.connections, 0)

    def test_hello_with_more_work_starts_sync(self):
        self.node.on_connect(1, "h", False)
        actions = self.node.on_message(1, hello_from(self.other, height=3, work=10**6))
        (get_blocks,) = sends_of(actions, GET_BLOCKS)
        self.assertEqual(get_blocks.message["from_index"], 1)
        self.assertTrue(self.node.syncing)
        self.assertEqual(self.node.stats["syncs"], 1)

    def test_hello_with_equal_work_does_not_sync(self):
        self.node.on_connect(1, "h", False)
        actions = self.node.on_message(1, hello_from(self.other, tip_hash="a" * 64))
        self.assertEqual(sends_of(actions, GET_BLOCKS), [])
        self.assertFalse(self.node.syncing)

    def test_invalid_miner_address(self):
        with self.assertRaises(ValueError):
            Node(miner_address="alice")


class PeerExchangeTests(unittest.TestCase):
    def setUp(self):
        self.node = Node(node_id="me", listen_port=5000, max_peers=3)
        self.node.on_connect(1, "10.0.0.1", False)  # un pair du réseau local : il peut nous parler d'adresses 10.x
        self.node.on_message(1, Node(node_id="other", listen_port=6000).hello())

    def test_connects_to_unknown_addresses(self):
        actions = self.node.on_message(1, message(PEERS, addresses=["10.0.0.5:5000", "10.0.0.6:5000"]))
        self.assertEqual(actions, [Connect("10.0.0.5:5000"), Connect("10.0.0.6:5000")])
        self.assertIn("10.0.0.5:5000", self.node.known_addresses)

    def test_ignores_known_and_own_addresses(self):
        actions = self.node.on_message(1, message(PEERS, addresses=["10.0.0.1:6000", "127.0.0.1:5000", "localhost:5000"]))
        self.assertEqual(actions, [])

    def test_respects_max_peers(self):
        for peer_id in (2, 3, 4):  # max_peers compte les connexions SORTANTES (les entrantes ont leur propre plafond)
            self.node.on_connect(peer_id, f"10.0.0.{peer_id}", True, f"10.0.0.{peer_id}:5000")
        actions = self.node.on_message(1, message(PEERS, addresses=["10.0.0.5:5000"]))
        self.assertEqual(actions, [])
        self.assertIn("10.0.0.5:5000", self.node.known_addresses)  # mémorisée quand même

    def test_malformed_address_disconnects(self):
        only_disconnect(self.node.on_message(1, message(PEERS, addresses=["pas une adresse"])), "mal formé")

    def test_address_book_is_bounded(self):
        for i in range(node_module.MAX_KNOWN_ADDRESSES + 10):
            self.node.on_message(1, message(PEERS, addresses=[f"10.1.{i // 250}.{i % 250}:1"]))
        self.assertEqual(len(self.node.known_addresses), node_module.MAX_KNOWN_ADDRESSES)


class AccountQueryTests(unittest.TestCase):
    def test_account_reply_includes_projection(self):
        clock = FakeClock(START)
        node = Node(node_id="me", chain=chain_with_blocks(1, clock), clock=clock, min_fee=0)
        node.on_connect(1, "h", False)
        node.on_message(1, Node(node_id="client").hello())
        node.submit_transaction(signed_tx(MINER, ALICE, coins(10)))
        (reply,) = node.on_message(1, message(GET_ACCOUNT, address=MINER.address))
        self.assertEqual(reply.message.type, ACCOUNT)
        self.assertEqual(reply.message["balance"], coins(50))
        self.assertEqual(reply.message["next_sequence"], 0)
        self.assertEqual(reply.message["projected_balance"], coins(40))
        self.assertEqual(reply.message["projected_next_sequence"], 1)
        self.assertEqual(reply.message["height"], 1)
        only_disconnect(node.on_message(1, message(GET_ACCOUNT, address="alice")), "mal formé")

    def test_account_and_reject_are_ignored(self):
        node = Node(node_id="me")
        node.on_connect(1, "h", False)
        node.on_message(1, Node(node_id="x").hello())
        self.assertEqual(node.on_message(1, message(REJECT, hash="h", reason="r")), [])
        reply = message(ACCOUNT, address="a", balance=0, next_sequence=0, projected_balance=0, projected_next_sequence=0, height=0)
        self.assertEqual(node.on_message(1, reply), [])


class ThreeNodeNetwork(unittest.TestCase):
    """A - B - C en ligne ; A mine. L'horloge est partagée et contrôlée."""

    def setUp(self):
        self.clock = FakeClock(START)
        self.net = SimulatedNetwork(self.clock)
        # min_fee=0 : les frais (Partie 11) sont hors sujet ici, voir test_fees.py.
        self.a = self.net.add(Node(node_id="A", miner_address=MINER.address, clock=self.clock, min_fee=0))
        self.b = self.net.add(Node(node_id="B", clock=self.clock, min_fee=0))
        self.c = self.net.add(Node(node_id="C", miner_address=MALLORY.address, clock=self.clock, min_fee=0))
        self.net.connect("A", "B")
        self.net.connect("B", "C")
        self.net.delivered.clear()

    def deliveries(self, type_):
        return [(d.sender, d.recipient) for d in self.net.delivered if d.type == type_]

    def heights(self):
        return (self.a.height, self.b.height, self.c.height)

    def test_discovery_connects_a_and_c(self):
        self.assertTrue(self.net.connected("A", "C"))
        self.assertEqual({p.node_id for p in self.b.peers}, {"A", "C"})

    def test_block_propagates_and_stops(self):
        block = self.net.mine("A")
        self.assertEqual(self.heights(), (1, 1, 1))
        self.assertEqual(self.b.tip, block)
        self.assertEqual(self.c.tip, block)
        # Chaque nœud relaie une fois ; personne ne renvoie le bloc à celui qui le lui a donné.
        relays = self.deliveries(NEW_BLOCK)
        self.assertEqual(len(relays), 4, relays)  # A->B, A->C, B->C, C->B (chacun l'a déjà : fin)
        self.assertNotIn(("B", "A"), relays)
        self.assertEqual(self.deliveries(GET_BLOCKS), [])

    def test_transaction_propagates_and_stops(self):
        self.net.mine("A")
        tx = signed_tx(MINER, ALICE, coins(10))
        self.net.delivered.clear()
        self.net.run("A", self.a.submit_transaction(tx))
        for node in (self.a, self.b, self.c):
            self.assertIn(tx, node.mempool)
        relays = self.deliveries(NEW_TRANSACTION)
        self.assertEqual(len(relays), 4, relays)
        self.assertEqual(self.deliveries(REJECT), [])
        self.assertEqual(self.net.run("A", self.a.submit_transaction(tx)), None)  # déjà connue : rien

    def test_transaction_is_mined_by_another_node(self):
        self.net.mine("A")
        self.net.run("B", self.b.submit_transaction(signed_tx(MINER, ALICE, coins(10))))
        self.clock.advance(TARGET_BLOCK_TIME)
        block = self.net.mine("C")
        self.assertEqual(len(block.transactions), 2)
        for node in (self.a, self.b, self.c):
            self.assertEqual(node.chain.state.balance_of(ALICE.address), coins(10))
            self.assertEqual(len(node.mempool), 0)

    def test_invalid_transaction_gets_reject_not_relay(self):
        self.net.mine("A")
        tx = signed_tx(ALICE, BOB, coins(1))  # alice n'a rien
        with self.assertRaises(InvalidTransactionError):
            self.a.submit_transaction(tx)
        peer_id = self.net.peer_id("A", "B")
        actions = self.b.on_message(self.net.peer_id("B", "A"), message(NEW_TRANSACTION, transaction=transaction_to_dict(tx)))
        (reject,) = actions
        self.assertEqual(reject.message.type, REJECT)
        self.assertIn("solde insuffisant", reject.message["reason"])
        self.assertNotIn(tx, self.b.mempool)
        self.assertEqual(self.b.stats["transactions_rejected"], 1)
        self.assertIsNotNone(peer_id)

    def test_late_joiner_catches_up(self):
        for _ in range(3):
            self.clock.advance(TARGET_BLOCK_TIME)
            self.net.mine("A")
        d = self.net.add(Node(node_id="D", clock=self.clock))
        self.net.delivered.clear()
        self.net.connect("D", "C")
        self.assertEqual(d.height, 3)
        self.assertEqual(d.chain.blocks, self.a.chain.blocks)
        self.assertEqual(self.deliveries(GET_BLOCKS), [("D", "C")])
        self.assertEqual(d.stats["blocks_accepted"], 3)
        self.assertEqual(d.stats["reorganizations"], 0)
        self.assertTrue(self.net.connected("D", "A"))  # découverte via C

    def test_catch_up_is_paginated(self):
        with mock.patch.object(node_module, "MAX_BLOCKS_PER_MESSAGE", 2):
            for _ in range(5):
                self.clock.advance(TARGET_BLOCK_TIME)
                self.net.mine("A")
            d = self.net.add(Node(node_id="D", clock=self.clock))
            self.net.delivered.clear()
            self.net.connect("D", "C")
        self.assertEqual(d.height, 5)
        self.assertEqual(self.deliveries(GET_BLOCKS), [("D", "C")] * 3)  # 2 + 2 + 1 blocs

    def test_fork_same_height_keeps_first_seen_then_heavier_wins(self):
        self.net.mine("A")
        self.clock.advance(TARGET_BLOCK_TIME)
        # Réseau coupé : A et C trouvent chacun un bloc n°2 au même instant (même travail).
        self.net.partition("A", "B")
        self.net.partition("A", "C")
        block_a = self.net.mine("A", coinbase_data="branche A")
        paid = signed_tx(MINER, ALICE, coins(5))
        self.net.run("C", self.c.submit_transaction(paid))
        block_c = self.net.mine("C", coinbase_data="branche C")
        self.assertNotEqual(block_a, block_c)
        self.assertEqual((self.a.tip, self.b.tip, self.c.tip), (block_a, block_c, block_c))
        self.net.heal("A", "B")
        self.net.heal("A", "C")
        # A mine le bloc n°3 : sa branche devient la plus lourde, B et C basculent.
        self.clock.advance(TARGET_BLOCK_TIME)
        block_a3 = self.net.mine("A")
        self.assertEqual(self.heights(), (3, 3, 3))
        self.assertEqual(self.c.tip, block_a3)
        self.assertEqual(self.c.chain.block_at(2), block_a)
        self.assertEqual(self.c.stats["reorganizations"], 1)
        self.assertEqual(self.b.stats["reorganizations"], 1)
        # La transaction confirmée dans le bloc abandonné de C est de retour dans les mempools.
        self.assertIn(paid, self.c.mempool)
        self.assertIn(paid, self.b.mempool)
        self.assertEqual(self.c.chain.state.balance_of(ALICE.address), 0)
        self.assertNotIn(paid, self.a.mempool)  # A ne l'a jamais reçue (coupure) : personne ne la re-diffuse
        # Le prochain bloc de C la confirme sur la bonne branche.
        self.clock.advance(TARGET_BLOCK_TIME)
        self.net.mine("C")
        for node in (self.a, self.b, self.c):
            self.assertEqual(node.chain.state.balance_of(ALICE.address), coins(5))
        self.assertEqual(self.heights(), (4, 4, 4))

    def test_equal_work_fork_is_not_adopted(self):
        self.net.mine("A")
        self.clock.advance(TARGET_BLOCK_TIME)
        self.net.partition("A", "B")
        self.net.partition("A", "C")
        block_a = self.net.mine("A")
        block_c = self.net.mine("C")
        self.net.heal("A", "B")
        self.net.heal("A", "C")
        # C annonce sa pointe à A : A demande le bloc n°2 de C, le compare, égalité => garde le sien.
        self.net.delivered.clear()
        self.net.run("A", self.a.on_message(self.net.peer_id("A", "C"), message(NEW_BLOCK, block=block_to_dict(block_c))))
        self.assertEqual(self.deliveries(GET_BLOCKS), [("A", "C")])
        self.assertEqual(self.a.tip, block_a)
        self.assertEqual(self.c.tip, block_c)
        self.assertEqual(self.a.stats["branches_lighter"], 1)
        self.assertFalse(self.a.syncing)

    def test_longer_but_lighter_branch_loses(self):
        """Une chaîne plus LONGUE mais de moindre travail cumulé ne remplace pas la nôtre."""
        self.net.mine("A")
        fork_point = self.a.tip
        # Branche honnête : 2 blocs rapides (difficulté croissante).
        for _ in range(2):
            self.clock.advance(1)
            self.net.mine("A")
        # Mallory, isolée, fabrique 3 blocs LENTS (difficulté décroissante de 12,5 % à chaque bloc).
        attacker = Blockchain.from_blocks(self.a.chain.blocks[: fork_point.index + 1])
        timestamp = fork_point.timestamp
        for _ in range(3):
            timestamp += TARGET_BLOCK_TIME + 1
            attacker.add_block(mined(create_block(attacker.last_block, [], MALLORY.address, timestamp=timestamp)))
        self.assertGreater(len(attacker), len(self.a.chain))
        self.assertLess(attacker.total_work, self.a.work)
        self.clock.advance(100)
        # Mallory annonce sa pointe à B ; B télécharge sa branche jusqu'au point de divergence, la pèse, la refuse.
        self.b.on_connect(99, "h", False)
        self.b.on_message(99, Node(node_id="M0", chain=attacker).hello())
        actions = self.b.on_message(99, message(NEW_BLOCK, block=block_to_dict(attacker.last_block)))
        requests = []
        while actions:
            (request,) = actions
            requests.append(request.message["from_index"])
            batch = attacker.blocks_from(request.message["from_index"], 100)
            actions = self.b.on_message(99, message(BLOCKS, blocks=blocks_to_list(batch), has_more=False))
        self.assertEqual(requests, [4, 3, 1])
        self.assertEqual(self.b.stats["branches_lighter"], 1)
        self.assertEqual(self.b.height, 3)
        self.assertEqual(self.b.tip, self.a.tip)
        # Et si Mallory rejoint le réseau, c'est elle qui adopte la chaîne honnête, plus lourde.
        mallory = self.net.add(Node(node_id="M", chain=attacker, clock=self.clock))
        self.net.connect("M", "B")
        self.assertEqual(mallory.tip, self.a.tip)
        self.assertEqual(mallory.stats["reorganizations"], 1)

    def test_majority_attacker_rewrites_history(self):
        """Limite honnête : plus de travail cumulé gagne, même pour réécrire un paiement confirmé."""
        self.net.mine("A")
        fork_point = self.a.tip
        self.clock.advance(TARGET_BLOCK_TIME)
        self.net.run("A", self.a.submit_transaction(signed_tx(MINER, BOB, coins(20))))
        self.net.mine("A")
        self.assertEqual(self.b.chain.state.balance_of(BOB.address), coins(20))
        # Mallory (plus de puissance : elle produit 3 blocs rapides pendant que le réseau en fait 1)
        # repart d'avant le paiement, qui n'existe pas dans sa branche.
        attacker = Blockchain.from_blocks(self.a.chain.blocks[: fork_point.index + 1])
        timestamp = fork_point.timestamp
        for _ in range(3):
            timestamp += 1
            attacker.add_block(mined(create_block(attacker.last_block, [], MALLORY.address, timestamp=timestamp)))
        self.assertGreater(attacker.total_work, self.a.work)
        self.net.add(Node(node_id="M", chain=attacker, clock=self.clock))
        self.net.connect("M", "B")
        self.assertEqual(self.heights(), (4, 4, 4))
        for node in (self.a, self.b, self.c):
            self.assertEqual(node.tip, attacker.last_block)
            self.assertEqual(node.chain.state.balance_of(BOB.address), 0)  # paiement effacé de l'historique
            self.assertEqual(node.stats["reorganizations"], 1)


class BlockAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.node = Node(node_id="me", miner_address=MINER.address, clock=self.clock, min_fee=0)
        self.node.on_connect(1, "h", False)
        self.node.on_message(1, Node(node_id="peer").hello())
        self.node.on_connect(2, "h", False)
        self.node.on_message(2, Node(node_id="peer2").hello())

    def announce(self, block):
        return self.node.on_message(1, message(NEW_BLOCK, block=block_to_dict(block)))

    def next_block(self, timestamp=None, transactions=(), miner=MINER):
        if timestamp is None:
            timestamp = self.node.tip.timestamp + TARGET_BLOCK_TIME
        return mined(create_block(self.node.tip, list(transactions), miner.address, timestamp=timestamp))

    def test_valid_block_is_adopted_and_relayed_to_others(self):
        block = self.next_block()
        actions = self.announce(block)
        self.assertEqual(self.node.tip, block)
        self.assertEqual(actions, [Send(2, message(NEW_BLOCK, block=block_to_dict(block)))])
        self.assertEqual(self.announce(block), [])  # déjà connu

    def test_invalid_block_disconnects_peer(self):
        block = self.next_block()
        greedy = replace(block.coinbase, amount=block.coinbase.amount + 1)
        greedy = replace(greedy, hash=greedy.calculate_hash())
        forged = mined(replace(block, transactions=(greedy,)))
        only_disconnect(self.announce(forged), "bloc invalide")
        self.assertEqual(self.node.height, 0)
        self.assertEqual(self.node.stats["blocks_rejected"], 1)

    def test_future_block_is_refused_without_disconnect(self):
        too_far = self.next_block(timestamp=self.clock.now + MAX_FUTURE_DRIFT_SECONDS + 1)
        self.assertEqual(self.announce(too_far), [])
        self.assertEqual(self.node.height, 0)
        self.assertEqual(self.node.stats["blocks_future"], 1)
        self.assertEqual(len(self.node.peers), 2)
        limit = self.next_block(timestamp=self.clock.now + MAX_FUTURE_DRIFT_SECONDS)
        self.announce(limit)
        self.assertEqual(self.node.tip, limit)

    def test_future_block_becomes_acceptable_later(self):
        block = self.next_block(timestamp=self.clock.now + MAX_FUTURE_DRIFT_SECONDS + 30)
        self.announce(block)
        self.assertEqual(self.node.height, 0)
        self.clock.advance(30)
        self.announce(block)
        self.assertEqual(self.node.height, 1)

    def test_future_block_in_sync_batch_is_refused(self):
        far = self.next_block(timestamp=self.clock.now + MAX_FUTURE_DRIFT_SECONDS + 1)
        beyond = mined(create_block(far, [], MINER.address, timestamp=far.timestamp + 1))
        self.announce(beyond)  # ne prolonge pas notre pointe : get_blocks
        self.assertTrue(self.node.syncing)
        self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list([far, beyond]), has_more=False))
        self.assertEqual(self.node.height, 0)
        self.assertEqual(self.node.stats["blocks_future"], 1)

    def test_unlinked_block_triggers_single_sync(self):
        block1 = self.next_block()
        block2 = mined(create_block(block1, [], MINER.address, timestamp=block1.timestamp + 1))
        (get_blocks,) = self.announce(block2)
        self.assertEqual(get_blocks, Send(1, message(GET_BLOCKS, from_index=1)))
        self.assertEqual(self.announce(block2), [])  # déjà en cours
        self.assertEqual(self.node.stats["syncs"], 1)

    def test_get_blocks_replies_with_pagination_flag(self):
        for _ in range(3):
            self.node.chain.add_block(self.next_block())
        (reply,) = self.node.on_message(1, message(GET_BLOCKS, from_index=2))
        self.assertEqual(reply.message.type, BLOCKS)
        self.assertEqual([b["index"] for b in reply.message["blocks"]], [2, 3])
        self.assertFalse(reply.message["has_more"])
        (reply,) = self.node.on_message(1, message(GET_BLOCKS, from_index=9))
        self.assertEqual(reply.message["blocks"], [])
        with mock.patch.object(node_module, "MAX_BLOCKS_PER_MESSAGE", 2):
            (reply,) = self.node.on_message(1, message(GET_BLOCKS, from_index=1))
        self.assertEqual([b["index"] for b in reply.message["blocks"]], [1, 2])
        self.assertTrue(reply.message["has_more"])

    def test_unsolicited_blocks_are_ignored(self):
        block = self.next_block()
        self.assertEqual(self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list([block]), has_more=False)), [])
        self.assertEqual(self.node.height, 0)

    def test_empty_batch_ends_sync(self):
        self.node.on_connect(3, "h", False)
        (request,) = self.node.on_message(3, hello_from(Node(node_id="x"), height=5, work=10**9))
        self.assertEqual(request.message["from_index"], 1)
        self.assertTrue(self.node.syncing)
        self.node.on_message(3, message(BLOCKS, blocks=[], has_more=False))
        self.assertFalse(self.node.syncing)

    def test_one_sync_at_a_time(self):
        self.node.on_connect(3, "h", False)
        self.node.on_message(3, hello_from(Node(node_id="x"), height=5, work=10**9))
        self.node.on_connect(4, "h", False)
        actions = self.node.on_message(4, hello_from(Node(node_id="y"), height=5, work=10**9, listen_port=None))
        self.assertEqual(sends_of(actions, GET_BLOCKS), [])
        self.assertEqual(self.node.stats["syncs"], 1)
        self.node.on_disconnect(3)  # le pair en cours disparaît : la voie est libre
        self.assertFalse(self.node.syncing)

    def test_sync_starts_at_peer_height_when_peer_is_shorter(self):
        for _ in range(4):
            self.node.chain.add_block(self.next_block())
        self.node.on_connect(3, "h", False)
        (request,) = self.node.on_message(3, hello_from(Node(node_id="x"), height=2, work=10**9))
        self.assertEqual(request.message["from_index"], 2)

    def test_incoherent_batch_disconnects(self):
        block1 = self.next_block()
        block2 = mined(create_block(block1, [], MINER.address, timestamp=block1.timestamp + 1))
        self.announce(block2)
        only_disconnect(
            self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list([block2]), has_more=False)), "incohérent"
        )
        self.assertFalse(self.node.syncing)

    def test_sync_backs_off_to_find_fork_point(self):
        """Fork profond de 3 blocs : demandes à h+1, puis h, puis h-2 (recul 1, 2)."""
        for _ in range(4):
            self.node.chain.add_block(self.next_block())
        common = self.node.chain.blocks[:2]  # Genesis + bloc 1
        other = Blockchain.from_blocks(common)
        timestamp = common[-1].timestamp
        for _ in range(5):
            timestamp += 1
            other.add_block(mined(create_block(other.last_block, [], MALLORY.address, timestamp=timestamp)))
        self.assertGreater(other.total_work, self.node.work)
        requests = []
        actions = self.announce(other.last_block)
        while actions:
            (request,) = actions
            self.assertEqual(request.message.type, GET_BLOCKS)
            requests.append(request.message["from_index"])
            batch = other.blocks_from(request.message["from_index"], 100)
            actions = self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list(batch), has_more=False))
            actions = [a for a in actions if not (isinstance(a, Send) and a.message.type == NEW_BLOCK)]
        self.assertEqual(requests, [5, 4, 2])
        self.assertEqual(self.node.chain.blocks, other.blocks)
        self.assertEqual(self.node.stats["reorganizations"], 1)

    def test_foreign_genesis_disconnects(self):
        fake_genesis = mined(replace(create_genesis_block(), timestamp=GENESIS_TIMESTAMP + 1, nonce=0))
        other = [fake_genesis]
        timestamp = fake_genesis.timestamp
        for _ in range(3):
            timestamp += 1
            other.append(mined(create_block(other[-1], [], MALLORY.address, timestamp=timestamp)))
        (request,) = self.announce(other[-1])
        self.assertEqual(request.message["from_index"], 1)  # dès le bloc 1 : il n'y a pas plus bas
        only_disconnect(
            self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list(other[1:]), has_more=False)), "Genesis"
        )

    def test_invalid_block_in_heavier_branch_disconnects(self):
        block1 = self.next_block()
        block2 = mined(create_block(block1, [signed_tx(ALICE, BOB, coins(1))], MINER.address, timestamp=block1.timestamp + 1))
        block3 = mined(create_block(block2, [], MINER.address, timestamp=block2.timestamp + 1))
        self.announce(block3)
        only_disconnect(
            self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list([block1, block2, block3]), has_more=False)),
            "invalide",
        )
        self.assertEqual(self.node.height, 1)  # le bloc 1, valide, a été gardé ; le bloc 2 (alice sans fonds) non

    def test_sync_bound(self):
        with mock.patch.object(node_module, "MAX_SYNC_BLOCKS", 1):
            block1 = self.next_block()
            block2 = mined(create_block(block1, [], MINER.address, timestamp=block1.timestamp + 1))
            self.announce(block2)
            only_disconnect(
                self.node.on_message(1, message(BLOCKS, blocks=blocks_to_list([block1, block2]), has_more=False)),
                "synchronisation",
            )


class LocalMiningTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.node = Node(node_id="me", miner_address=MINER.address, clock=self.clock, min_fee=0)

    def test_build_candidate_uses_clock_and_mempool(self):
        self.node.chain.add_block(mined(self.node.build_candidate("premier")))
        self.assertEqual(self.node.tip.timestamp, START)
        self.assertEqual(self.node.tip.coinbase.data, "premier")
        self.node.submit_transaction(signed_tx(MINER, ALICE, coins(1)))
        self.clock.advance(TARGET_BLOCK_TIME)
        candidate = self.node.build_candidate()
        self.assertEqual(candidate.timestamp, START + TARGET_BLOCK_TIME)
        self.assertEqual(len(candidate.transactions), 2)

    def test_candidate_timestamp_is_after_tip_even_if_clock_lags(self):
        self.node.chain.add_block(mined(self.node.build_candidate()))
        candidate = self.node.build_candidate()
        self.assertEqual(candidate.timestamp, self.node.tip.timestamp + 1)

    def test_no_miner_no_candidate(self):
        self.assertIsNone(Node(node_id="x").build_candidate())

    def test_submit_block_adopts_and_broadcasts(self):
        self.node.on_connect(1, "h", False)
        self.node.on_message(1, Node(node_id="peer").hello())
        block = mined(self.node.build_candidate())
        actions = self.node.submit_block(block)
        self.assertEqual(self.node.tip, block)
        self.assertEqual(actions, [Send(1, message(NEW_BLOCK, block=block_to_dict(block)))])

    def test_stale_block_is_dropped(self):
        stale = mined(self.node.build_candidate())
        self.node.chain.add_block(mined(self.node.build_candidate("autre")))
        self.assertEqual(self.node.submit_block(stale), [])
        self.assertEqual(self.node.stats["blocks_stale"], 1)

    def test_locally_invalid_block_raises(self):
        with self.assertRaises(InvalidBlockError):
            self.node.submit_block(self.node.build_candidate())  # non miné

    def test_submit_transaction_rejects_invalid(self):
        with self.assertRaises(InvalidTransactionError):
            self.node.submit_transaction(signed_tx(ALICE, BOB, 1))


class ReorganizationMempoolTests(unittest.TestCase):
    def test_resync_keeps_applicable_drops_confirmed_elsewhere(self):
        clock = FakeClock(START)
        chain = chain_with_blocks(1, clock)
        pool = Mempool(min_fee=0)
        state = chain.state
        keep = signed_tx(MINER, ALICE, coins(1), sequence=0)
        pool.add(keep, state)
        # Nouvel état où le mineur a déjà émis sa séquence 0 vers bob : `keep` devient un rejeu.
        elsewhere = signed_tx(MINER, BOB, coins(2), sequence=0)
        new_state = state.apply_transaction(elsewhere)
        returned = (signed_tx(MINER, CAROL, coins(3), sequence=1), elsewhere)
        dropped = pool.resync(new_state, returned)
        self.assertEqual(pool.transactions, (returned[0],))
        self.assertEqual(set(dropped), {keep, elsewhere})

    def test_work_is_the_criterion_not_length(self):
        fast = chain_with_blocks(3, FakeClock(START), spacing=1)
        slow = chain_with_blocks(4, FakeClock(START), spacing=TARGET_BLOCK_TIME + 1)
        self.assertGreater(len(slow), len(fast))
        self.assertGreater(chain_work(fast.blocks), chain_work(slow.blocks))
