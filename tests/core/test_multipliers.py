import unittest

from custom_components.electricity_price_scheduler.core.multipliers import resolve_multipliers


class MultiplierTests(unittest.TestCase):
    def test_fixed_settings_are_passed_through(self):
        result = resolve_multipliers(preheat_factor=1.3, recovery_factor=1.1)
        self.assertEqual((result.preheat_factor, result.recovery_factor), (1.3, 1.1))
        self.assertEqual(result.source, "configured")

    def test_legacy_temperature_bands_and_fallback(self):
        expected = ((7, 1.08, 1.04), (2, 1.14, 1.08), (-3, 1.24, 1.12), (-10, 1.34, 1.15), (-11, 1.45, 1.2))
        for temp, pre, rec in expected:
            with self.subTest(temp=temp):
                result = resolve_multipliers(mode="outdoor_temperature", temperature_c=temp)
                self.assertEqual((result.preheat_factor, result.recovery_factor), (pre, rec))
        fallback = resolve_multipliers(mode="outdoor_temperature", temperature_c=None)
        self.assertEqual((fallback.preheat_factor, fallback.recovery_factor), (1.5, 1.2))
        self.assertIsNotNone(fallback.warning)

    def test_invalid_factor_and_mode_raise(self):
        with self.assertRaises(ValueError):
            resolve_multipliers(preheat_factor=float("inf"))
        with self.assertRaises(ValueError):
            resolve_multipliers(mode="unknown")


if __name__ == "__main__":
    unittest.main()
