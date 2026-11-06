import unittest

from bank import transfer


class TransferTest(unittest.TestCase):
    def test_moves_money(self):
        accounts = {"a": 10, "b": 0}
        transfer(accounts, "a", "b", 4)
        self.assertEqual(accounts, {"a": 6, "b": 4})

    def test_insufficient_funds_leaves_accounts_unchanged(self):
        accounts = {"a": 1, "b": 0}
        with self.assertRaises(ValueError):
            transfer(accounts, "a", "b", 5)
        self.assertEqual(accounts, {"a": 1, "b": 0})


if __name__ == "__main__":
    unittest.main()
