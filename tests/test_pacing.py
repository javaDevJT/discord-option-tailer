import math
import unittest
from unittest.mock import patch

from relay.pacing import discord_delay


class DiscordDelayTests(unittest.TestCase):
    def test_delay_stays_within_twenty_percent_bounds(self):
        for _ in range(100):
            delay = discord_delay(5)
            self.assertGreaterEqual(delay, 4)
            self.assertLessEqual(delay, 6)

    def test_rng_can_make_delay_deterministic(self):
        self.assertEqual(discord_delay(10, rng=lambda lower, upper: lower), 8)
        self.assertEqual(discord_delay(10, rng=lambda lower, upper: upper), 12)

    def test_random_uniform_is_patchable(self):
        with patch("relay.pacing.random.uniform", return_value=5.5) as uniform:
            self.assertEqual(discord_delay(5), 5.5)
        uniform.assert_called_once_with(4.0, 6.0)

    def test_base_must_be_positive_and_finite(self):
        for invalid in (0, -1, math.inf, -math.inf, math.nan, "nope"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    discord_delay(invalid)

    def test_rng_result_must_preserve_bounds(self):
        with self.assertRaises(ValueError):
            discord_delay(5, rng=lambda lower, upper: upper + 1)


if __name__ == "__main__":
    unittest.main()
