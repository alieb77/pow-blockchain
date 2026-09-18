import unittest
from dataclasses import replace

from powchain.block import (
    GENESIS_DIFFICULTY,
    GENESIS_INDEX,
    GENESIS_NONCE,
    GENESIS_PREV_HASH,
    GENESIS_TIMESTAMP,
    INITIAL_NONCE,
    Block,
    calculate_transactions_hash,
    create_block,
    create_genesis_block,
    is_valid_block,
    validate_block,
)
from powchain.crypto import is_valid_hash_hex
from powchain.errors import InvalidBlockError
from powchain.mining import mine_block
from powchain.proof_of_work import TARGET_BLOCK_TIME, expected_difficulty, hash_meets_target
from powchain.transaction import Transaction
from tests.helpers import ALICE, BOB, CAROL, signed_tx

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


def mined(candidate):
    return mine_block(candidate).block


class GenesisTests(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(create_genesis_block(), create_genesis_block())

    def test_conventions(self):
        genesis = create_genesis_block()
        self.assertEqual(genesis.index, GENESIS_INDEX)
        self.assertEqual(genesis.timestamp, GENESIS_TIMESTAMP)
        self.assertEqual(genesis.transactions, ())
        self.assertEqual(genesis.prev_hash, GENESIS_PREV_HASH)
        self.assertEqual(genesis.difficulty, GENESIS_DIFFICULTY)
        self.assertEqual(genesis.nonce, GENESIS_NONCE)
        self.assertEqual(genesis.hash, genesis.calculate_hash())

    def test_satisfies_proof_of_work(self):
        genesis = create_genesis_block()
        self.assertTrue(hash_meets_target(genesis.hash, genesis.difficulty))
        self.assertTrue(genesis.has_valid_proof_of_work())
        # Le nonce figé est bien la PREMIÈRE solution : re-miner depuis 0 la retrouve.
        self.assertEqual(mined(replace(genesis, nonce=0)).nonce, GENESIS_NONCE)

    def test_is_valid_without_previous_block(self):
        self.assertTrue(is_valid_block(create_genesis_block()))

    def test_pinned_reference_hashes(self):
        genesis = create_genesis_block()
        self.assertEqual(genesis.hash, EXPECTED_GENESIS_HASH)
        self.assertEqual(genesis.calculate_transactions_hash(), EXPECTED_EMPTY_TRANSACTIONS_HASH)


class BlockHashTests(unittest.TestCase):
    def setUp(self):
        self.block = create_block(create_genesis_block(), sample_transactions(), timestamp=TIMESTAMP)

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
        _, tx2 = self.block.transactions
        altered_tx = signed_tx(ALICE, BOB, 150_000_001)
        altered = replace(self.block, transactions=(altered_tx, tx2))
        self.assertNotEqual(altered.calculate_hash(), self.block.hash)

    def test_transaction_order_changes_hash(self):
        tx1, tx2 = self.block.transactions
        reordered = replace(self.block, transactions=(tx2, tx1))
        self.assertNotEqual(reordered.calculate_hash(), self.block.hash)

    def test_removing_a_transaction_changes_hash(self):
        tx1, _ = self.block.transactions
        self.assertNotEqual(replace(self.block, transactions=(tx1,)).calculate_hash(), self.block.hash)

    def test_stored_hash_is_not_part_of_the_computation(self):
        self.assertEqual(replace(self.block, hash="f" * 64).calculate_hash(), self.block.hash)

    def test_transactions_hash_of_empty_list_is_stable(self):
        self.assertEqual(calculate_transactions_hash(()), calculate_transactions_hash([]))
        self.assertTrue(is_valid_hash_hex(calculate_transactions_hash(())))


class CreateBlockTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()

    def test_candidate_links_to_previous_block(self):
        candidate = create_block(self.genesis, sample_transactions(), timestamp=TIMESTAMP)
        self.assertEqual(candidate.index, self.genesis.index + 1)
        self.assertEqual(candidate.prev_hash, self.genesis.hash)
        self.assertEqual(candidate.nonce, INITIAL_NONCE)
        self.assertEqual(candidate.timestamp, TIMESTAMP)
        self.assertEqual(candidate.difficulty, GENESIS_DIFFICULTY)
        self.assertEqual(candidate.hash, candidate.calculate_hash())

    def test_candidate_is_rejected_until_mined(self):
        candidate = create_block(self.genesis, sample_transactions(), timestamp=TIMESTAMP)
        with self.assertRaisesRegex(InvalidBlockError, "preuve de travail"):
            validate_block(candidate, self.genesis)
        self.assertTrue(is_valid_block(mined(candidate), self.genesis))

    def test_default_timestamp_is_after_previous(self):
        candidate = create_block(self.genesis, [])
        self.assertIsInstance(candidate.timestamp, int)
        self.assertGreater(candidate.timestamp, self.genesis.timestamp)

    def test_difficulty_follows_adjustment_rule(self):
        cases = {
            "rapide": GENESIS_TIMESTAMP + 1,
            "exact": GENESIS_TIMESTAMP + TARGET_BLOCK_TIME,
            "lent": GENESIS_TIMESTAMP + 100,
        }
        for label, timestamp in cases.items():
            with self.subTest(case=label):
                candidate = create_block(self.genesis, [], timestamp=timestamp)
                self.assertEqual(
                    candidate.difficulty,
                    expected_difficulty(self.genesis.difficulty, self.genesis.timestamp, timestamp),
                )
        self.assertGreater(create_block(self.genesis, [], timestamp=GENESIS_TIMESTAMP + 1).difficulty, GENESIS_DIFFICULTY)
        self.assertLess(create_block(self.genesis, [], timestamp=GENESIS_TIMESTAMP + 100).difficulty, GENESIS_DIFFICULTY)

    def test_rejects_timestamp_not_after_previous(self):
        for timestamp in (GENESIS_TIMESTAMP, GENESIS_TIMESTAMP - 1, -1, 1.5, "10"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(InvalidBlockError):
                    create_block(self.genesis, [], timestamp=timestamp)

    def test_transactions_stored_as_tuple(self):
        candidate = create_block(self.genesis, list(sample_transactions()), timestamp=TIMESTAMP)
        self.assertIsInstance(candidate.transactions, tuple)

    def test_empty_block_is_allowed(self):
        block = mined(create_block(self.genesis, [], timestamp=TIMESTAMP))
        self.assertTrue(is_valid_block(block, self.genesis))

    def test_rejects_invalid_transaction(self):
        bad = Transaction(ALICE.address, BOB.address, -1, "", 0, "0" * 64, "0" * 128)
        with self.assertRaises(InvalidBlockError):
            create_block(self.genesis, [bad], timestamp=TIMESTAMP)

    def test_rejects_duplicate_transaction(self):
        tx = signed_tx(ALICE, BOB, 1)
        with self.assertRaisesRegex(InvalidBlockError, "dupliquée"):
            create_block(self.genesis, [tx, tx], timestamp=TIMESTAMP)

    def test_rejects_non_block_previous(self):
        with self.assertRaises(InvalidBlockError):
            create_block("genesis", [], timestamp=TIMESTAMP)
        with self.assertRaises(InvalidBlockError):
            create_block(replace(self.genesis, difficulty=0), [], timestamp=TIMESTAMP)


class ValidateBlockTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()
        self.block = mined(create_block(self.genesis, sample_transactions(), timestamp=TIMESTAMP))

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
        stale = Block(1, GENESIS_TIMESTAMP, (), self.genesis.hash, GENESIS_DIFFICULTY, 0, "0" * 64)
        with self.assertRaisesRegex(InvalidBlockError, "strictement supérieur"):
            validate_block(mined(stale), self.genesis)

    def test_wrong_index(self):
        forged = mined(replace(self.block, index=2))
        with self.assertRaisesRegex(InvalidBlockError, "index"):
            validate_block(forged, self.genesis)

    def test_tampered_transaction_inside_block(self):
        tx1, tx2 = self.block.transactions
        forged_tx = replace(tx1, amount=tx1.amount * 2)
        forged = replace(self.block, transactions=(forged_tx, tx2))
        with self.assertRaisesRegex(InvalidBlockError, "transaction n°0 invalide"):
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


if __name__ == "__main__":
    unittest.main()
