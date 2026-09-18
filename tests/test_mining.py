import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block, is_valid_block
from powchain.errors import MiningError, MiningLimitError
from powchain.mining import MiningResult, mine_block
from powchain.proof_of_work import TARGET_BLOCK_TIME, hash_meets_target
from tests.helpers import ALICE, BOB, MINER, signed_tx

TIMESTAMP = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME


class MineBlockTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()
        self.candidate = create_block(
            self.genesis, [signed_tx(ALICE, BOB, 1)], MINER.address, timestamp=TIMESTAMP
        )

    def test_mined_block_meets_target_and_hash_is_consistent(self):
        result = mine_block(self.candidate)
        self.assertIsInstance(result, MiningResult)
        self.assertTrue(hash_meets_target(result.block.hash, result.block.difficulty))
        # Le hash incrémental du mineur est identique au hash canonique complet.
        self.assertEqual(result.block.hash, result.block.calculate_hash())
        self.assertGreaterEqual(result.attempts, 1)
        self.assertEqual(result.block.nonce, self.candidate.nonce + result.attempts - 1)

    def test_only_nonce_and_hash_change(self):
        mined = mine_block(self.candidate).block
        self.assertEqual(replace(mined, nonce=self.candidate.nonce, hash=self.candidate.hash), self.candidate)

    def test_mined_block_is_valid_against_previous(self):
        self.assertTrue(is_valid_block(mine_block(self.candidate).block, self.genesis))

    def test_mining_is_deterministic(self):
        first = mine_block(self.candidate)
        second = mine_block(self.candidate)
        self.assertEqual(first.block, second.block)
        self.assertEqual(first.attempts, second.attempts)

    def test_starts_from_candidate_nonce(self):
        first = mine_block(self.candidate).block
        later = mine_block(replace(self.candidate, nonce=first.nonce + 1)).block
        self.assertGreater(later.nonce, first.nonce)
        self.assertTrue(hash_meets_target(later.hash, later.difficulty))

    def test_difficulty_one_needs_a_single_attempt(self):
        result = mine_block(replace(self.candidate, difficulty=1))
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.block.nonce, self.candidate.nonce)

    def test_max_attempts_limit(self):
        needed = mine_block(self.candidate).attempts
        self.assertEqual(mine_block(self.candidate, max_attempts=needed).attempts, needed)
        if needed > 1:
            with self.assertRaises(MiningLimitError):
                mine_block(self.candidate, max_attempts=needed - 1)

    def test_hash_rate_is_reported(self):
        result = mine_block(self.candidate)
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)
        self.assertGreaterEqual(result.hash_rate, 0.0)

    def test_rejects_unusable_candidates(self):
        with self.assertRaises(MiningError):
            mine_block("bloc")
        with self.assertRaises(MiningError):
            mine_block(replace(self.candidate, difficulty=0))
        with self.assertRaises(MiningError):
            mine_block(replace(self.candidate, nonce=-1))
        with self.assertRaises(MiningError):
            mine_block(replace(self.candidate, prev_hash="xyz"))
        with self.assertRaises(MiningError):
            mine_block(self.candidate, max_attempts=0)


if __name__ == "__main__":
    unittest.main()
