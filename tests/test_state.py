import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block
from powchain.errors import InvalidBlockError, InvalidTransactionError
from powchain.money import MAX_MONEY, block_reward
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.state import EMPTY_ACCOUNT, Account, State
from powchain.transaction import create_coinbase_transaction
from tests.helpers import ALICE, BOB, MINER, coins, signed_tx

REWARD = block_reward(1)


def funded_state():
    """Le mineur a reçu une récompense (50 COIN) ; personne d'autre n'a rien."""
    return State().apply_transaction(create_coinbase_transaction(MINER.address, 1))


class AccountTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(Account(), Account(0, 0))
        self.assertEqual(EMPTY_ACCOUNT.balance, 0)
        self.assertEqual(EMPTY_ACCOUNT.next_sequence, 0)


class StateTests(unittest.TestCase):
    def test_empty_state(self):
        state = State()
        self.assertEqual(state.account(ALICE.address), EMPTY_ACCOUNT)
        self.assertEqual(state.balance_of(ALICE.address), 0)
        self.assertEqual(state.next_sequence_of(ALICE.address), 0)
        self.assertEqual(state.total_supply, 0)
        self.assertEqual(len(state.accounts), 0)

    def test_coinbase_creates_money(self):
        state = funded_state()
        self.assertEqual(state.balance_of(MINER.address), REWARD)
        self.assertEqual(state.next_sequence_of(MINER.address), 0)
        self.assertEqual(state.total_supply, REWARD)

    def test_transfer_debits_credits_and_increments_sequence(self):
        state = funded_state().apply_transaction(signed_tx(MINER, ALICE, coins(10), sequence=0))
        self.assertEqual(state.balance_of(MINER.address), REWARD - coins(10))
        self.assertEqual(state.balance_of(ALICE.address), coins(10))
        self.assertEqual(state.next_sequence_of(MINER.address), 1)
        self.assertEqual(state.next_sequence_of(ALICE.address), 0)
        self.assertEqual(state.total_supply, REWARD)  # un transfert ne crée rien

    def test_self_transfer_keeps_balance_and_increments_sequence(self):
        state = funded_state().apply_transaction(signed_tx(MINER, MINER, coins(10), sequence=0))
        self.assertEqual(state.balance_of(MINER.address), REWARD)
        self.assertEqual(state.next_sequence_of(MINER.address), 1)

    def test_wrong_sequence_is_rejected(self):
        with self.assertRaisesRegex(InvalidTransactionError, "sequence 1 .* attend la sequence 0"):
            funded_state().apply_transaction(signed_tx(MINER, ALICE, coins(1), sequence=1))

    def test_replay_is_rejected(self):
        tx = signed_tx(MINER, ALICE, coins(1), sequence=0)
        state = funded_state().apply_transaction(tx)
        with self.assertRaisesRegex(InvalidTransactionError, "sequence 0 .* attend la sequence 1"):
            state.apply_transaction(tx)

    def test_insufficient_balance_is_rejected(self):
        with self.assertRaisesRegex(InvalidTransactionError, "solde insuffisant"):
            funded_state().apply_transaction(signed_tx(MINER, ALICE, REWARD + 1, sequence=0))
        with self.assertRaisesRegex(InvalidTransactionError, "solde insuffisant"):
            State().apply_transaction(signed_tx(ALICE, BOB, 1))

    def test_exact_balance_can_be_spent(self):
        state = funded_state().apply_transaction(signed_tx(MINER, ALICE, REWARD, sequence=0))
        self.assertEqual(state.balance_of(MINER.address), 0)

    def test_state_is_immutable(self):
        before = funded_state()
        after = before.apply_transaction(signed_tx(MINER, ALICE, coins(1), sequence=0))
        self.assertEqual(before.balance_of(MINER.address), REWARD)
        self.assertEqual(before.next_sequence_of(MINER.address), 0)
        self.assertNotEqual(before, after)
        with self.assertRaises(TypeError):
            before.accounts[ALICE.address] = Account(1, 0)

    def test_supply_cap_guard(self):
        rich = State({MINER.address: Account(MAX_MONEY, 0)})
        with self.assertRaisesRegex(InvalidTransactionError, "MAX_MONEY"):
            rich.apply_transaction(create_coinbase_transaction(MINER.address, 1))

    def test_rejects_non_transaction(self):
        with self.assertRaises(InvalidTransactionError):
            State().apply_transaction("tx")
        with self.assertRaises(InvalidBlockError):
            State().apply_block("bloc")

    def test_apply_block_applies_all_transactions_in_order(self):
        genesis = create_genesis_block()
        block = create_block(
            genesis,
            [signed_tx(MINER, ALICE, coins(10), sequence=0), signed_tx(MINER, BOB, coins(5), sequence=1)],
            MINER.address,
            timestamp=GENESIS_TIMESTAMP + TARGET_BLOCK_TIME,
        )
        # Non miné : l'état ne regarde pas la preuve de travail, seulement les transactions.
        state = State().apply_block(block)
        self.assertEqual(state.balance_of(MINER.address), REWARD - coins(15))
        self.assertEqual(state.balance_of(ALICE.address), coins(10))
        self.assertEqual(state.balance_of(BOB.address), coins(5))

    def test_apply_block_reports_failing_position(self):
        genesis = create_genesis_block()
        block = create_block(genesis, [], MINER.address, timestamp=GENESIS_TIMESTAMP + TARGET_BLOCK_TIME)
        block = replace(block, transactions=block.transactions + (signed_tx(ALICE, BOB, coins(1)),))
        with self.assertRaisesRegex(InvalidBlockError, "bloc n°1 : transaction n°1 .* solde insuffisant"):
            State().apply_block(block)

    def test_equality_and_repr(self):
        self.assertEqual(funded_state(), funded_state())
        self.assertNotEqual(funded_state(), State())
        self.assertIn("1 comptes", repr(funded_state()))
        self.assertIn("50.00000000 FLS", repr(funded_state()))


if __name__ == "__main__":
    unittest.main()
