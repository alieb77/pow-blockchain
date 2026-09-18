"""Partie 11 : frais de transaction, du champ signé jusqu'à la coinbase et au mempool."""

import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block, validate_block
from powchain.chain import Blockchain
from powchain.codec import transaction_from_dict, transaction_to_dict
from powchain.errors import InvalidBlockError, InvalidTransactionError, MempoolError
from powchain.mempool import Mempool
from powchain.mining import mine_block
from powchain.money import MAX_MONEY, MIN_RELAY_FEE, block_reward
from powchain.node import Node
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.protocol import NEW_TRANSACTION, REJECT, message
from powchain.simulation import FakeClock
from powchain.state import State
from powchain.transaction import (
    create_coinbase_transaction,
    create_signed_transaction,
    is_valid_transaction,
    validate_transaction,
)
from tests.helpers import ALICE, BOB, CAROL, MINER, coins, mined, signed_tx

START = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME
FEE = MIN_RELAY_FEE
REWARD = block_reward(1)


def funded_state() -> State:
    """Le mineur vient de toucher sa première récompense (50 COIN, séquence 0)."""
    return State().apply_transaction(create_coinbase_transaction(MINER.address, 1))


def rich_state() -> State:
    """Le mineur (séquence 2, 30 COIN) a déjà financé alice et bob de 10 COIN chacun."""
    state = funded_state()
    state = state.apply_transaction(signed_tx(MINER, ALICE, coins(10), sequence=0))
    return state.apply_transaction(signed_tx(MINER, BOB, coins(10), sequence=1))


class TransactionFeeTests(unittest.TestCase):
    def test_fee_is_signed_and_part_of_the_hash(self):
        cheap = signed_tx(ALICE, BOB, coins(1), fee=1)
        dear = signed_tx(ALICE, BOB, coins(1), fee=2)
        self.assertNotEqual(cheap.hash, dear.hash)
        self.assertNotEqual(cheap.signature, dear.signature)
        tampered = replace(cheap, fee=0)
        self.assertFalse(is_valid_transaction(tampered))  # hash stocké périmé
        tampered = replace(tampered, hash=tampered.calculate_hash())
        with self.assertRaisesRegex(InvalidTransactionError, "signature invalide"):
            validate_transaction(tampered)  # hash recalculé, mais la signature couvrait l'ancien frais

    def test_fee_bounds(self):
        for bad in (-1, 1.5, True, None, MAX_MONEY + 1):
            with self.subTest(fee=bad):
                with self.assertRaisesRegex(InvalidTransactionError, "fee invalide"):
                    create_signed_transaction(ALICE, BOB.address, 1, fee=bad)
        self.assertEqual(create_signed_transaction(ALICE, BOB.address, 1, fee=0).fee, 0)

    def test_coinbase_collects_fees_and_pays_none(self):
        coinbase = create_coinbase_transaction(MINER.address, 3, fees=1234)
        self.assertEqual(coinbase.fee, 0)
        self.assertEqual(coinbase.amount, block_reward(3) + 1234)
        validate_transaction(coinbase)
        with self.assertRaisesRegex(InvalidTransactionError, "frais collectés"):
            create_coinbase_transaction(MINER.address, 3, fees=-1)
        greedy = replace(coinbase, fee=5)
        greedy = replace(greedy, hash=greedy.calculate_hash())
        with self.assertRaisesRegex(InvalidTransactionError, "coinbase : fee"):
            validate_transaction(greedy)

    def test_codec_round_trip_keeps_fee(self):
        tx = signed_tx(ALICE, BOB, coins(1), fee=FEE)
        data = transaction_to_dict(tx)
        self.assertEqual(data["fee"], FEE)
        self.assertEqual(transaction_from_dict(data), tx)


class StateFeeTests(unittest.TestCase):
    def test_sender_pays_amount_plus_fee_recipient_gets_amount(self):
        state = funded_state().apply_transaction(signed_tx(MINER, ALICE, coins(10), fee=FEE))
        self.assertEqual(state.balance_of(MINER.address), REWARD - coins(10) - FEE)
        self.assertEqual(state.balance_of(ALICE.address), coins(10))
        # Les frais ne sont crédités à personne ici : ils reviennent au mineur via la coinbase.
        self.assertEqual(state.total_supply, REWARD - FEE)

    def test_fee_counts_toward_the_balance_check(self):
        funded_state().apply_transaction(signed_tx(MINER, ALICE, REWARD, fee=0))  # solde exact : passe
        with self.assertRaisesRegex(InvalidTransactionError, r"solde insuffisant.*frais"):
            funded_state().apply_transaction(signed_tx(MINER, ALICE, REWARD, fee=1))


class BlockFeeTests(unittest.TestCase):
    def setUp(self):
        self.chain = Blockchain()
        self.chain.add_block(mined(create_block(self.chain.last_block, [], MINER.address, timestamp=START)))
        self.next_timestamp = START + TARGET_BLOCK_TIME

    def test_coinbase_equals_reward_plus_fees(self):
        txs = [
            signed_tx(MINER, ALICE, coins(1), fee=FEE, sequence=0),
            signed_tx(MINER, BOB, coins(1), fee=3 * FEE, sequence=1),
        ]
        block = mined(create_block(self.chain.last_block, txs, MINER.address, timestamp=self.next_timestamp))
        self.assertEqual(block.total_fees, 4 * FEE)
        self.assertEqual(block.coinbase.amount, block_reward(2) + 4 * FEE)
        self.chain.add_block(block)
        state = self.chain.state
        self.assertEqual(state.balance_of(MINER.address), REWARD + block_reward(2) - coins(2))  # frais récupérés
        self.assertEqual(state.balance_of(ALICE.address), coins(1))
        self.assertEqual(state.total_supply, REWARD + block_reward(2))  # les frais ne créent rien

    def test_coinbase_must_collect_exactly_the_fees(self):
        txs = (signed_tx(MINER, ALICE, coins(1), fee=FEE),)
        template = create_block(self.chain.last_block, txs, MINER.address, timestamp=self.next_timestamp)
        for delta in (+1, -1, -FEE):
            with self.subTest(delta=delta):
                coinbase = replace(template.coinbase, amount=template.coinbase.amount + delta)
                coinbase = replace(coinbase, hash=coinbase.calculate_hash())
                forged = mine_block(replace(template, transactions=(coinbase,) + txs)).block
                with self.assertRaisesRegex(InvalidBlockError, "récompense .*frais"):
                    validate_block(forged, self.chain.last_block)

    def test_genesis_and_fee_free_blocks_are_unchanged(self):
        self.assertEqual(self.chain.blocks[0], create_genesis_block())
        self.assertEqual(self.chain.blocks[1].coinbase.amount, REWARD)
        self.assertEqual(self.chain.blocks[1].total_fees, 0)


class MempoolFeeTests(unittest.TestCase):
    def setUp(self):
        self.state = rich_state()
        self.pool = Mempool()  # politique par défaut : MIN_RELAY_FEE

    def test_default_policy_rejects_cheap_transactions(self):
        self.assertEqual(self.pool.min_fee, MIN_RELAY_FEE)
        with self.assertRaisesRegex(MempoolError, "frais insuffisants"):
            self.pool.add(signed_tx(ALICE, CAROL, coins(1), fee=MIN_RELAY_FEE - 1), self.state)
        self.pool.add(signed_tx(ALICE, CAROL, coins(1), fee=MIN_RELAY_FEE), self.state)
        self.assertEqual(len(self.pool), 1)

    def test_policy_is_configurable(self):
        with self.assertRaises(ValueError):
            Mempool(min_fee=-1)
        free = Mempool(min_fee=0)
        free.add(signed_tx(ALICE, CAROL, coins(1), fee=0), self.state)
        self.assertEqual(len(free), 1)

    def test_select_serves_the_best_payers_first(self):
        cheap = signed_tx(ALICE, CAROL, coins(1), fee=FEE)
        mid = signed_tx(MINER, CAROL, coins(1), fee=2 * FEE, sequence=2)
        dear = signed_tx(BOB, CAROL, coins(1), fee=5 * FEE)
        for tx in (cheap, mid, dear):
            self.pool.add(tx, self.state)
        self.assertEqual(self.pool.transactions, (cheap, mid, dear))  # ordre d'arrivée conservé
        self.assertEqual(self.pool.select(self.state), (dear, mid, cheap))  # servi par frais
        self.assertEqual(self.pool.select(self.state, max_transactions=1), (dear,))

    def test_select_keeps_sequence_order_within_a_sender(self):
        first = signed_tx(MINER, CAROL, coins(1), fee=FEE, sequence=2)
        second = signed_tx(MINER, CAROL, coins(1), fee=10 * FEE, sequence=3)  # paie plus, mais après la n°2
        self.pool.add(first, self.state)
        self.pool.add(second, self.state)
        self.assertEqual(self.pool.select(self.state), (first, second))
        self.assertEqual(self.pool.select(self.state, max_transactions=1), (first,))

    def test_equal_fees_keep_arrival_order(self):
        a = signed_tx(ALICE, CAROL, coins(1), fee=FEE)
        b = signed_tx(BOB, CAROL, coins(1), fee=FEE)
        self.pool.add(b, self.state)
        self.pool.add(a, self.state)
        self.assertEqual(self.pool.select(self.state), (b, a))

    def test_full_mempool_evicts_the_cheapest_for_a_better_payer(self):
        pool = Mempool(max_size=2)
        cheap = signed_tx(ALICE, CAROL, coins(1), fee=FEE)
        mid = signed_tx(BOB, CAROL, coins(1), fee=2 * FEE)
        self.assertEqual(pool.add(cheap, self.state), ())
        pool.add(mid, self.state)
        with self.assertRaisesRegex(MempoolError, "plein"):  # même frais que la moins payante : refusée
            pool.add(signed_tx(MINER, CAROL, coins(1), fee=FEE, sequence=2), self.state)
        dear = signed_tx(MINER, CAROL, coins(1), fee=3 * FEE, sequence=2)
        self.assertEqual(pool.add(dear, self.state), (cheap,))
        self.assertEqual(pool.transactions, (mid, dear))

    def test_eviction_takes_the_senders_dependents_along(self):
        pool = Mempool(max_size=2)
        first = signed_tx(MINER, CAROL, coins(1), fee=FEE, sequence=2)
        second = signed_tx(MINER, CAROL, coins(1), fee=5 * FEE, sequence=3)  # inapplicable sans first
        pool.add(first, self.state)
        pool.add(second, self.state)
        dear = signed_tx(ALICE, CAROL, coins(1), fee=3 * FEE)
        self.assertEqual(pool.add(dear, self.state), (first, second))
        self.assertEqual(pool.transactions, (dear,))

    def test_nothing_is_evicted_for_a_transaction_that_cannot_apply(self):
        pool = Mempool(max_size=1)
        pending = signed_tx(ALICE, CAROL, coins(1), fee=FEE)
        pool.add(pending, self.state)
        with self.assertRaisesRegex(InvalidTransactionError, "solde insuffisant"):
            pool.add(signed_tx(CAROL, BOB, coins(1), fee=9 * FEE), self.state)  # carol n'a rien
        self.assertEqual(pool.transactions, (pending,))

    def test_resync_applies_the_fee_policy(self):
        abandoned = signed_tx(ALICE, CAROL, coins(1), fee=0)  # miné ailleurs par un nœud plus laxiste
        kept = signed_tx(BOB, CAROL, coins(1), fee=FEE)
        self.assertEqual(self.pool.resync(self.state, (abandoned, kept)), (abandoned,))
        self.assertEqual(self.pool.transactions, (kept,))


class NodeFeeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(START)
        self.node = Node(node_id="me", miner_address=MINER.address, clock=self.clock)
        self.node.chain.add_block(mined(self.node.build_candidate()))
        self.clock.advance(TARGET_BLOCK_TIME)

    def test_cheap_transaction_gets_a_reject_by_default(self):
        self.node.on_connect(1, "h", False)
        self.node.on_message(1, Node(node_id="peer").hello())
        cheap = signed_tx(MINER, ALICE, coins(1), fee=0)
        (reject,) = self.node.on_message(1, message(NEW_TRANSACTION, transaction=transaction_to_dict(cheap)))
        self.assertEqual(reject.message.type, REJECT)
        self.assertIn("frais insuffisants", reject.message["reason"])
        self.assertNotIn(cheap, self.node.mempool)
        with self.assertRaises(MempoolError):
            self.node.submit_transaction(cheap)
        paid = signed_tx(MINER, ALICE, coins(1), fee=MIN_RELAY_FEE)
        self.node.submit_transaction(paid)
        self.assertIn(paid, self.node.mempool)

    def test_mined_block_pays_the_fees_to_the_miner(self):
        self.node.submit_transaction(signed_tx(MINER, ALICE, coins(1), fee=FEE, sequence=0))
        self.node.submit_transaction(signed_tx(MINER, BOB, coins(2), fee=2 * FEE, sequence=1))
        block = mined(self.node.build_candidate())
        self.node.submit_block(block)
        self.assertEqual(block.coinbase.amount, block_reward(2) + 3 * FEE)
        self.assertEqual(self.node.chain.state.balance_of(MINER.address), REWARD + block_reward(2) - coins(3))
        self.assertEqual(len(self.node.mempool), 0)


if __name__ == "__main__":
    unittest.main()
