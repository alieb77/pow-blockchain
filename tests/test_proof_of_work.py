import unittest

from powchain.proof_of_work import (
    ADJUSTMENT_DIVISOR,
    INITIAL_DIFFICULTY,
    MAX_DIFFICULTY,
    MAX_TARGET,
    MIN_DIFFICULTY,
    TARGET_BLOCK_TIME,
    block_work,
    expected_difficulty,
    format_target,
    hash_meets_target,
    is_valid_difficulty,
    target_from_difficulty,
)


class TargetTests(unittest.TestCase):
    def test_difficulty_one_accepts_every_hash(self):
        self.assertEqual(target_from_difficulty(1), MAX_TARGET)
        self.assertTrue(hash_meets_target("f" * 64, 1))
        self.assertTrue(hash_meets_target("0" * 64, 1))

    def test_target_halves_when_difficulty_doubles(self):
        self.assertEqual(target_from_difficulty(2), MAX_TARGET // 2)
        self.assertEqual(target_from_difficulty(4096), MAX_TARGET // 4096)

    def test_target_of_power_of_16_starts_with_zeros(self):
        target_hex = format_target(target_from_difficulty(16**3))
        self.assertEqual(len(target_hex), 64)
        self.assertTrue(target_hex.startswith("000"))
        self.assertEqual(target_hex[3], "f")

    def test_hash_meets_target_boundary(self):
        target = target_from_difficulty(4096)
        self.assertTrue(hash_meets_target(format_target(target), 4096))
        self.assertFalse(hash_meets_target(format_target(target + 1), 4096))

    def test_hash_meets_target_rejects_malformed_hash(self):
        for bad in ("", "0" * 63, "G" * 64, None, 12):
            with self.subTest(hash=bad):
                self.assertFalse(hash_meets_target(bad, 1))

    def test_difficulty_validation(self):
        for good in (MIN_DIFFICULTY, INITIAL_DIFFICULTY, MAX_DIFFICULTY):
            self.assertTrue(is_valid_difficulty(good))
        for bad in (0, -1, True, 1.0, "4096", None, MAX_DIFFICULTY + 1):
            with self.subTest(value=bad):
                self.assertFalse(is_valid_difficulty(bad))
                with self.assertRaises(ValueError):
                    target_from_difficulty(bad)


class ExpectedDifficultyTests(unittest.TestCase):
    def test_unchanged_when_exactly_on_target(self):
        self.assertEqual(expected_difficulty(4096, 0, TARGET_BLOCK_TIME), 4096)

    def test_increases_by_one_step_when_faster(self):
        step = 4096 // ADJUSTMENT_DIVISOR
        self.assertEqual(expected_difficulty(4096, 0, 1), 4096 + step)
        self.assertEqual(expected_difficulty(4096, 0, TARGET_BLOCK_TIME - 1), 4096 + step)

    def test_decreases_by_one_step_when_slower(self):
        step = 4096 // ADJUSTMENT_DIVISOR
        self.assertEqual(expected_difficulty(4096, 0, TARGET_BLOCK_TIME + 1), 4096 - step)
        self.assertEqual(expected_difficulty(4096, 0, 10**9), 4096 - step)

    def test_step_is_at_least_one(self):
        self.assertEqual(expected_difficulty(4, 0, 1), 5)
        self.assertEqual(expected_difficulty(4, 0, 100), 3)

    def test_clamped_to_bounds(self):
        self.assertEqual(expected_difficulty(MIN_DIFFICULTY, 0, 100), MIN_DIFFICULTY)
        self.assertEqual(expected_difficulty(MAX_DIFFICULTY, 0, 1), MAX_DIFFICULTY)

    def test_uses_previous_timestamp_as_origin(self):
        self.assertEqual(
            expected_difficulty(4096, 1000, 1000 + TARGET_BLOCK_TIME),
            expected_difficulty(4096, 0, TARGET_BLOCK_TIME),
        )

    def test_rejects_non_increasing_timestamp(self):
        with self.assertRaises(ValueError):
            expected_difficulty(4096, 10, 10)
        with self.assertRaises(ValueError):
            expected_difficulty(4096, 10, 9)

    def test_rejects_invalid_previous_difficulty(self):
        for bad in (0, -1, True, 1.0):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    expected_difficulty(bad, 0, 1)


class WorkTests(unittest.TestCase):
    def test_block_work_equals_difficulty(self):
        self.assertEqual(block_work(1), 1)
        self.assertEqual(block_work(4096), 4096)

    def test_block_work_rejects_invalid_difficulty(self):
        with self.assertRaises(ValueError):
            block_work(0)


if __name__ == "__main__":
    unittest.main()
