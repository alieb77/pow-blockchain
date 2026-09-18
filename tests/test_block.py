import unittest
from dataclasses import replace

from powchain.block import (
    GENESIS_DIFFICULTY,
    GENESIS_INDEX,
    GENESIS_NONCE,
    GENESIS_PREV_HASH,
    GENESIS_TIMESTAMP,
    INITIAL_NONCE,
    MAX_TRANSACTIONS_PER_BLOCK,
    Block,
    calculate_transactions_hash,
    create_block,
    create_genesis_block,
    is_valid_block,
    validate_block,
)
from powchain.crypto import is_valid_hash_hex
from powchain.errors import InvalidBlockError
from powchain.money import block_reward
from powchain.proof_of_work import TARGET_BLOCK_TIME, expected_difficulty, hash_meets_target
from powchain.transaction import COINBASE_ADDRESS, Transaction, create_coinbase_transaction
from tests.helpers import ALICE, BOB, CAROL, MINER, mined, signed_tx

# Écart exactement égal à la cible : la difficulté reste celle du Genesis.
TIMESTAMP = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME

# Vecteurs de référence du format canonique v2 (bloc avec difficulté, Genesis miné).
# Si l'un de ces tests casse, c'est que la sérialisation ou les constantes du
# Genesis ont changé : TOUS les hashes de blocs changent alors, ce qui doit
# être une décision explicite.
EXPECTED_GENESIS_HASH = "000cb9d400f03a2aa00056576b6d77cba0f781cad0c5b26fae824ae2de93facd"
EXPECTED_EMPTY_TRANSACTIONS_HASH = "c71076c68a94fc7e89b8934a7ec1aac1142a2de96dea79ec8fb6a1e156ebc452"


def sample_transactions():
    return (
        signed_tx(ALICE, BOB, 150_000_000),
        signed_tx(BOB, CAROL, 25_000_000, "note"),
    )


def candidate_block(prev_block, transactions=(), timestamp=TIMESTAMP, **kwargs):
    return create_block(prev_block, transactions, MINER.address, timestamp=timestamp, **kwargs)


class GenesisTests(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(create_genesis_block(), create_genesis_block())

    def test_conventions(self):
        genesis = create_genesis_block()
        self.assertEqual(genesis.index, GENESIS_INDEX)
        self.assertEqual(genesis.timestamp, GENESIS_TIMESTAMP)
        self.assertEqual(genesis.transactions, ())
        self.assertIsNone(genesis.coinbase)
        self.assertEqual(genesis.prev_hash, GENESIS_PREV_HASH)
        self.assertEqual(genesis.difficulty, GENESIS_DIFFICULTY)
        self.assertEqual(genesis.nonce, GENESIS_NONCE)
        self.assertEqual(genesis.hash, genesis.calculate_hash())

    def test_satisfies_proof_of_work(self):
        genesis = create_genesis_block()
        self.assertTrue(hash_meets_target(genesis.hash, genesis.difficulty))
        self.assertTrue(genesis.has_valid_proof_of_work())
        self.assertEqual(mined(replace(genesis, nonce=0)).nonce, GENESIS_NONCE)

    def test_is_valid_without_previous_block(self):
        self.assertTrue(is_valid_block(create_genesis_block()))

    def test_pinned_reference_hashes(self):
        genesis = create_genesis_block()
        self.assertEqual(genesis.hash, EXPECTED_GENESIS_HASH)
        self.assertEqual(genesis.calculate_transactions_hash(), EXPECTED_EMPTY_TRANSACTIONS_HASH)


class BlockHashTests(unittest.TestCase):
    def setUp(self):
        self.block = candidate_block(create_genesis_block(), sample_transactions())
        self.coinbase, self.tx1, self.tx2 = self.block.transactions

    def test_hash_is_canonical_hex(self):
        self.assertTrue(is_valid_hash_hex(self.block.hash))

    def test_each_header_field_changes_hash(self):
        changes = {
            "index": 2,
            "timestamp": TIMESTAMP + 1,
            "prev_hash": "1" * 64,
            "difficulty": self.block.difficulty + 1,
            "nonce": 1,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                altered = replace(self.block, **{field: value})
                self.assertNotEqual(altered.calculate_hash(), self.block.hash)

    def test_transaction_content_changes_hash(self):
        altered_tx = signed_tx(ALICE, BOB, 150_000_001)
        altered = replace(self.block, transactions=(self.coinbase, altered_tx, self.tx2))
        self.assertNotEqual(altered.calculate_hash(), self.block.hash)

    def test_transaction_order_changes_hash(self):
        reordered = replace(self.block, transactions=(self.coinbase, self.tx2, self.tx1))
        self.assertNotEqual(reordered.calculate_hash(), self.block.hash)

    def test_removing_a_transaction_changes_hash(self):
        shorter = replace(self.block, transactions=(self.coinbase, self.tx1))
        self.assertNotEqual(shorter.calculate_hash(), self.block.hash)

    def test_stored_hash_is_not_part_of_the_computation(self):
        self.assertEqual(replace(self.block, hash="f" * 64).calculate_hash(), self.block.hash)

    def test_transactions_hash_of_empty_list_is_stable(self):
        self.assertEqual(calculate_transactions_hash(()), calculate_transactions_hash([]))
        self.assertTrue(is_valid_hash_hex(calculate_transactions_hash(())))


class CreateBlockTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()

    def test_candidate_links_to_previous_block_and_starts_with_coinbase(self):
        candidate = candidate_block(self.genesis, sample_transactions())
        self.assertEqual(candidate.index, self.genesis.index + 1)
        self.assertEqual(candidate.prev_hash, self.genesis.hash)
        self.assertEqual(candidate.nonce, INITIAL_NONCE)
        self.assertEqual(candidate.timestamp, TIMESTAMP)
        self.assertEqual(candidate.difficulty, GENESIS_DIFFICULTY)
        self.assertEqual(candidate.hash, candidate.calculate_hash())
        self.assertEqual(len(candidate.transactions), 3)
        coinbase = candidate.coinbase
        self.assertIsNotNone(coinbase)
        self.assertIs(coinbase, candidate.transactions[0])
        self.assertEqual(coinbase.sender, COINBASE_ADDRESS)
        self.assertEqual(coinbase.recipient, MINER.address)
        self.assertEqual(coinbase.amount, block_reward(1))
        self.assertEqual(coinbase.sequence, 1)
        self.assertFalse(coinbase.is_signed)

    def test_coinbase_data_is_carried(self):
        candidate = candidate_block(self.genesis, coinbase_data="hello miner")
        self.assertEqual(candidate.coinbase.data, "hello miner")

    def test_candidate_is_rejected_until_mined(self):
        candidate = candidate_block(self.genesis, sample_transactions())
        with self.assertRaisesRegex(InvalidBlockError, "preuve de travail"):
            validate_block(candidate, self.genesis)
        self.assertTrue(is_valid_block(mined(candidate), self.genesis))

    def test_default_timestamp_is_after_previous(self):
        candidate = create_block(self.genesis, [], MINER.address)
        self.assertIsInstance(candidate.timestamp, int)
        self.assertGreater(candidate.timestamp, self.genesis.timestamp)

    def test_difficulty_follows_adjustment_rule(self):
        for label, timestamp in {"rapide": GENESIS_TIMESTAMP + 1, "exact": TIMESTAMP, "lent": GENESIS_TIMESTAMP + 100}.items():
            with self.subTest(case=label):
                candidate = candidate_block(self.genesis, timestamp=timestamp)
                self.assertEqual(
                    candidate.difficulty,
                    expected_difficulty(self.genesis.difficulty, self.genesis.timestamp, timestamp),
                )
        self.assertGreater(candidate_block(self.genesis, timestamp=GENESIS_TIMESTAMP + 1).difficulty, GENESIS_DIFFICULTY)
        self.assertLess(candidate_block(self.genesis, timestamp=GENESIS_TIMESTAMP + 100).difficulty, GENESIS_DIFFICULTY)

    def test_rejects_timestamp_not_after_previous(self):
        for timestamp in (GENESIS_TIMESTAMP, GENESIS_TIMESTAMP - 1, -1, 1.5, "10"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(InvalidBlockError):
                    candidate_block(self.genesis, timestamp=timestamp)

    def test_transactions_stored_as_tuple(self):
        candidate = candidate_block(self.genesis, list(sample_transactions()))
        self.assertIsInstance(candidate.transactions, tuple)

    def test_block_with_only_coinbase_is_valid(self):
        block = mined(candidate_block(self.genesis))
        self.assertEqual(len(block.transactions), 1)
        self.assertTrue(is_valid_block(block, self.genesis))

    def test_rejects_invalid_transaction(self):
        bad = Transaction(ALICE.address, BOB.address, -1, 0, "", 0, "0" * 64, "0" * 128)
        with self.assertRaisesRegex(InvalidBlockError, "transaction n°1 invalide"):
            candidate_block(self.genesis, [bad])

    def test_rejects_duplicate_transaction(self):
        tx = signed_tx(ALICE, BOB, 1)
        with self.assertRaisesRegex(InvalidBlockError, "dupliquée"):
            candidate_block(self.genesis, [tx, tx])

    def test_rejects_user_supplied_coinbase(self):
        with self.assertRaisesRegex(InvalidBlockError, "une seule coinbase"):
            candidate_block(self.genesis, [create_coinbase_transaction(ALICE.address, 1)])

    def test_rejects_invalid_miner_address(self):
        for bad in ("alice", "", None, ALICE.address.upper()):
            with self.subTest(miner=bad):
                with self.assertRaisesRegex(InvalidBlockError, "coinbase"):
                    create_block(self.genesis, [], bad, timestamp=TIMESTAMP)

    def test_rejects_non_block_previous(self):
        with self.assertRaises(InvalidBlockError):
            create_block("genesis", [], MINER.address, timestamp=TIMESTAMP)
        with self.assertRaises(InvalidBlockError):
            create_block(replace(self.genesis, difficulty=0), [], MINER.address, timestamp=TIMESTAMP)


class ValidateBlockTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()
        self.block = mined(candidate_block(self.genesis, sample_transactions()))
        self.coinbase, self.tx1, self.tx2 = self.block.transactions

    def test_valid_block(self):
        validate_block(self.block, self.genesis)
        self.assertTrue(is_valid_block(self.block, self.genesis))

    def test_detects_header_tampering_without_recompute(self):
        changes = {
            "index": 5,
            "timestamp": TIMESTAMP + 1,
            "difficulty": self.block.difficulty + 1,
            "nonce": self.block.nonce + 1,
            "prev_hash": "1" * 64,
            "hash": "0" * 64,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(is_valid_block(replace(self.block, **{field: value}), self.genesis))

    def test_remined_block_is_accepted_but_broken_link_is_not(self):
        renonced = mined(replace(self.block, nonce=self.block.nonce + 1))
        self.assertTrue(is_valid_block(renonced, self.genesis))
        wrong_prev = mined(replace(self.block, prev_hash="1" * 64))
        with self.assertRaisesRegex(InvalidBlockError, "prev_hash"):
            validate_block(wrong_prev, self.genesis)

    def test_wrong_difficulty_is_rejected_even_if_mined(self):
        forged = mined(replace(self.block, difficulty=self.block.difficulty + 1))
        with self.assertRaisesRegex(InvalidBlockError, "difficulté .* attendue"):
            validate_block(forged, self.genesis)

    def test_rewrite_without_mining_is_rejected(self):
        rewritten = replace(self.block, nonce=0)
        rewritten = replace(rewritten, hash=rewritten.calculate_hash())
        with self.assertRaisesRegex(InvalidBlockError, "preuve de travail"):
            validate_block(rewritten, self.genesis)

    def test_timestamp_must_be_after_previous(self):
        coinbase = create_coinbase_transaction(MINER.address, 1)
        stale = Block(1, GENESIS_TIMESTAMP, (coinbase,), self.genesis.hash, GENESIS_DIFFICULTY, 0, "0" * 64)
        with self.assertRaisesRegex(InvalidBlockError, "strictement supérieur"):
            validate_block(mined(stale), self.genesis)

    def test_wrong_index(self):
        coinbase_for_height_2 = create_coinbase_transaction(MINER.address, 2)
        forged = mined(replace(self.block, index=2, transactions=(coinbase_for_height_2, self.tx1, self.tx2)))
        with self.assertRaisesRegex(InvalidBlockError, "index incohérent"):
            validate_block(forged, self.genesis)

    def test_tampered_transaction_inside_block(self):
        forged_tx = replace(self.tx1, amount=self.tx1.amount * 2)
        forged = replace(self.block, transactions=(self.coinbase, forged_tx, self.tx2))
        with self.assertRaisesRegex(InvalidBlockError, "transaction n°1 invalide"):
            validate_block(forged, self.genesis)

    def test_transactions_must_be_a_tuple(self):
        as_list = replace(self.block, transactions=list(self.block.transactions))
        self.assertFalse(is_valid_block(as_list, self.genesis))

    def test_bad_field_types(self):
        changes = {
            "index": "1",
            "timestamp": 1.0,
            "difficulty": 0,
            "nonce": True,
            "prev_hash": None,
            "hash": 12,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(is_valid_block(replace(self.block, **{field: value}), self.genesis))

    def test_non_genesis_block_without_previous(self):
        with self.assertRaisesRegex(InvalidBlockError, "précédent"):
            validate_block(self.block, None)

    def test_index_zero_requires_genesis_prev_hash(self):
        fake = mined(Block(0, GENESIS_TIMESTAMP, (), "1" * 64, GENESIS_DIFFICULTY, 0, "0" * 64))
        self.assertFalse(is_valid_block(fake))

    def test_non_block_object(self):
        self.assertFalse(is_valid_block("bloc"))
        self.assertFalse(is_valid_block(self.block, "genesis"))


class CoinbaseRuleTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()
        self.block = mined(candidate_block(self.genesis, sample_transactions()))
        self.coinbase, self.tx1, self.tx2 = self.block.transactions

    def assert_rejected(self, transactions, pattern):
        forged = mined(replace(self.block, transactions=transactions))
        with self.assertRaisesRegex(InvalidBlockError, pattern):
            validate_block(forged, self.genesis)

    def test_missing_coinbase(self):
        self.assert_rejected((self.tx1, self.tx2), "coinbase")

    def test_coinbase_must_be_first(self):
        self.assert_rejected((self.tx1, self.coinbase, self.tx2), "première transaction")

    def test_only_one_coinbase(self):
        second = create_coinbase_transaction(ALICE.address, 1)
        self.assert_rejected((self.coinbase, self.tx1, second), "une seule coinbase")

    def test_inflated_reward(self):
        greedy = replace(self.coinbase, amount=self.coinbase.amount + 1)
        greedy = replace(greedy, hash=greedy.calculate_hash())
        self.assert_rejected((greedy, self.tx1, self.tx2), "récompense")

    def test_reduced_reward_is_also_rejected(self):
        modest = replace(self.coinbase, amount=self.coinbase.amount - 1)
        modest = replace(modest, hash=modest.calculate_hash())
        self.assert_rejected((modest, self.tx1, self.tx2), "récompense")

    def test_coinbase_sequence_must_equal_height(self):
        wrong_height = create_coinbase_transaction(MINER.address, 2)
        self.assert_rejected((wrong_height, self.tx1, self.tx2), "hauteur")

    def test_signed_coinbase_is_rejected(self):
        signed = replace(self.coinbase, signature="a" * 128)
        self.assert_rejected((signed, self.tx1, self.tx2), "signature")

    def test_genesis_must_not_have_a_coinbase(self):
        fake = Block(0, GENESIS_TIMESTAMP, (create_coinbase_transaction(MINER.address, 1),), GENESIS_PREV_HASH, GENESIS_DIFFICULTY, 0, "0" * 64)
        with self.assertRaisesRegex(InvalidBlockError, "Genesis"):
            validate_block(mined(fake))

    def test_too_many_transactions(self):
        oversized = (self.coinbase,) + (self.tx1,) * MAX_TRANSACTIONS_PER_BLOCK
        self.assert_rejected(oversized, "trop de transactions")


if __name__ == "__main__":
    unittest.main()
