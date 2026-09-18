import unittest

from powchain.money import (
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD,
    MAX_MONEY,
    UNITS_PER_COIN,
    block_reward,
    format_units,
    is_valid_amount,
    parse_coin_amount,
)


class AmountTests(unittest.TestCase):
    def test_parse_coin_amount(self):
        self.assertEqual(parse_coin_amount("1.5"), 150_000_000)
        self.assertEqual(parse_coin_amount("0.00000001"), 1)
        self.assertEqual(parse_coin_amount("10"), 10 * UNITS_PER_COIN)
        self.assertEqual(parse_coin_amount(" 2.25 "), 225_000_000)
        for bad in ("1.123456789", "abc", "-1", "", ".5", "1.", "1,5"):
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    parse_coin_amount(bad)
        with self.assertRaises(TypeError):
            parse_coin_amount(1.5)

    def test_format_units(self):
        self.assertEqual(format_units(150_000_000), "1.50000000 COIN")
        self.assertEqual(format_units(0), "0.00000000 COIN")
        self.assertEqual(format_units(1), "0.00000001 COIN")
        with self.assertRaises(ValueError):
            format_units(-1)

    def test_is_valid_amount(self):
        self.assertTrue(is_valid_amount(0))
        self.assertTrue(is_valid_amount(MAX_MONEY))
        for bad in (-1, MAX_MONEY + 1, True, 1.0, "1", None):
            with self.subTest(value=bad):
                self.assertFalse(is_valid_amount(bad))


class BlockRewardTests(unittest.TestCase):
    def test_schedule(self):
        self.assertEqual(block_reward(1), 50 * UNITS_PER_COIN)
        self.assertEqual(block_reward(HALVING_INTERVAL - 1), 50 * UNITS_PER_COIN)
        self.assertEqual(block_reward(HALVING_INTERVAL), 25 * UNITS_PER_COIN)
        self.assertEqual(block_reward(2 * HALVING_INTERVAL), 1_250_000_000)
        self.assertEqual(block_reward(3 * HALVING_INTERVAL), 625_000_000)
        self.assertEqual(block_reward(100 * HALVING_INTERVAL), 0)

    def test_rejects_invalid_heights(self):
        for bad in (0, -1, True, 1.0, "1"):
            with self.subTest(height=bad):
                with self.assertRaises(ValueError):
                    block_reward(bad)

    def test_total_emission_stays_under_cap(self):
        total = 0
        era = 0
        while True:
            reward = INITIAL_BLOCK_REWARD >> era
            if reward == 0:
                break
            total += reward * HALVING_INTERVAL
            era += 1
        self.assertLess(total, MAX_MONEY)
        self.assertGreater(total, MAX_MONEY * 99 // 100)


if __name__ == "__main__":
    unittest.main()
