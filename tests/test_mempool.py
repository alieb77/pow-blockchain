import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block
from powchain.errors import InvalidTransactionError, MempoolError
from powchain.mempool import Mempool
from powchain.money import block_reward
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.state import State
from powchain.transaction import create_coinbase_transaction, create_transaction
from tests.helpers import ALICE, BOB, CAROL, MINER, coins, mined, signed_tx

REWARD = block_reward(1)


def funded_state():
    return State().apply_transaction(create_coinbase_transaction(MINER.address, 1))


class MempoolAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = funded_state()
        self.pool = Mempool()

    def test_add_valid_transaction(self):
        tx = signed_tx(MINER, ALICE, coins(1))
        self.pool.add(tx, self.state)
        self.assertEqual(len(self.pool), 1)
        self.assertIn(tx, self.pool)
        self.assertIn(tx.hash, self.pool)
        self.assertEqual(self.pool.transactions, (tx,))

    def test_rejects_structurally_invalid_transaction(self):
        with self.assertRaisesRegex(InvalidTransactionError, "non signée"):
            self.pool.add(create_transaction(MINER.address, ALICE.address, 1), self.state)
        with self.assertRaises(InvalidTransactionError):
            self.pool.add("tx", self.state)
        self.assertEqual(len(self.pool), 0)

    def test_rejects_coinbase(self):
        with self.assertRaisesRegex(MempoolError, "coinbase"):
            self.pool.add(create_coinbase_transaction(ALICE.address, 2), self.state)

    def test_rejects_duplicate(self):
        tx = signed_tx(MINER, ALICE, coins(1))
        self.pool.add(tx, self.state)
        with self.assertRaisesRegex(MempoolError, "déjà en attente"):
            self.pool.add(tx, self.state)
        self.assertEqual(len(self.pool), 1)

    def test_rejects_wrong_sequence(self):
        with self.assertRaisesRegex(InvalidTransactionError, "sequence 1 .* attend la sequence 0"):
            self.pool.add(signed_tx(MINER, ALICE, coins(1), sequence=1), self.state)

    def test_rejects_insufficient_balance(self):
        with self.assertRaisesRegex(InvalidTransactionError, "solde insuffisant"):
            self.pool.add(signed_tx(ALICE, BOB, coins(1)), self.state)

    def test_rejects_replay_of_confirmed_transaction(self):
        tx = signed_tx(MINER, ALICE, coins(1), sequence=0)
        confirmed_state = self.state.apply_transaction(tx)
        with self.assertRaisesRegex(InvalidTransactionError, "sequence 0 .* attend la sequence 1"):
            self.pool.add(tx, confirmed_state)

    def test_chained_sequences_use_projected_state(self):
        self.pool.add(signed_tx(MINER, ALICE, coins(30), sequence=0), self.state)
        self.pool.add(signed_tx(MINER, BOB, coins(15), sequence=1), self.state)
        with self.assertRaisesRegex(InvalidTransactionError, "solde insuffisant"):
            self.pool.add(signed_tx(MINER, CAROL, coins(10), sequence=2), self.state)
        with self.assertRaisesRegex(InvalidTransactionError, "attend la sequence 2"):
            self.pool.add(signed_tx(MINER, CAROL, coins(1), sequence=3), self.state)
        self.pool.add(signed_tx(MINER, CAROL, coins(5), sequence=2), self.state)
        self.assertEqual(len(self.pool), 3)
        self.assertEqual(self.pool.projected_state(self.state).balance_of(MINER.address), 0)

    def test_pending_payment_funds_a_further_payment(self):
        # alice n'a rien dans l'état, mais un paiement en attente du mineur la finance.
        self.pool.add(signed_tx(MINER, ALICE, coins(10), sequence=0), self.state)
        self.pool.add(signed_tx(ALICE, BOB, coins(4), sequence=0), self.state)
        self.assertEqual(len(self.pool), 2)

    def test_full_mempool(self):
        pool = Mempool(max_size=1)
        pool.add(signed_tx(MINER, ALICE, coins(1), sequence=0), self.state)
        with self.assertRaisesRegex(MempoolError, "plein"):
            pool.add(signed_tx(MINER, BOB, coins(1), sequence=1), self.state)

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            Mempool(max_size=0)
        with self.assertRaises(TypeError):
            self.pool.add(signed_tx(MINER, ALICE, coins(1)), "state")


class MempoolSelectionTests(unittest.TestCase):
    def setUp(self):
        self.state = funded_state()
        self.pool = Mempool()
        self.tx_a = signed_tx(MINER, ALICE, coins(10), sequence=0)
        self.tx_b = signed_tx(MINER, BOB, coins(5), sequence=1)
        self.tx_c = signed_tx(ALICE, CAROL, coins(4), sequence=0)
        for tx in (self.tx_a, self.tx_b, self.tx_c):
            self.pool.add(tx, self.state)

    def test_select_respects_order_and_limit(self):
        self.assertEqual(self.pool.select(self.state), (self.tx_a, self.tx_b, self.tx_c))
        self.assertEqual(self.pool.select(self.state, max_transactions=2), (self.tx_a, self.tx_b))
        self.assertEqual(self.pool.select(self.state, max_transactions=0), ())

    def test_select_skips_transactions_invalid_on_new_state(self):
        # Un autre paiement du mineur (séquence 0) a été confirmé entre-temps :
        # tx_a est morte (séquence consommée), tx_b (séquence 1) reste valide,
        # tx_c dépend de tx_a et devient inapplicable.
        other = signed_tx(MINER, CAROL, coins(1), sequence=0)
        new_state = self.state.apply_transaction(other)
        self.assertEqual(self.pool.select(new_state), (self.tx_b,))

    def test_selected_transactions_make_a_valid_block(self):
        genesis = create_genesis_block()
        block = mined(create_block(genesis, self.pool.select(self.state), MINER.address, timestamp=GENESIS_TIMESTAMP + TARGET_BLOCK_TIME))
        self.assertEqual(len(block.transactions), 4)
        State().apply_block(block)  # ne lève pas

    def test_remove_confirmed_drops_included_and_prunes_stale(self):
        genesis = create_genesis_block()
        block = mined(create_block(genesis, [self.tx_a], MINER.address, timestamp=GENESIS_TIMESTAMP + TARGET_BLOCK_TIME))
        new_state = State().apply_block(block)
        dropped = self.pool.remove_confirmed(block, new_state)
        self.assertEqual(dropped, ())
        self.assertEqual(self.pool.transactions, (self.tx_b, self.tx_c))

    def test_remove_confirmed_prunes_transactions_made_invalid(self):
        genesis = create_genesis_block()
        competing = signed_tx(MINER, CAROL, coins(1), sequence=0)
        block = mined(create_block(genesis, [competing], MINER.address, timestamp=GENESIS_TIMESTAMP + TARGET_BLOCK_TIME))
        new_state = State().apply_block(block)
        dropped = self.pool.remove_confirmed(block, new_state)
        self.assertEqual(dropped, (self.tx_a, self.tx_c))
        self.assertEqual(self.pool.transactions, (self.tx_b,))

    def test_remove_confirmed_rejects_non_block(self):
        with self.assertRaises(TypeError):
            self.pool.remove_confirmed("bloc", self.state)


if __name__ == "__main__":
    unittest.main()
