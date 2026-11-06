import unittest

from durations import parse_duration


class ParseDurationTest(unittest.TestCase):
    def test_single_units(self):
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("2h"), 7200)

    def test_invalid(self):
        for text in ("", "5x", "m5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)


if __name__ == "__main__":
    unittest.main()
