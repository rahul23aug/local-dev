import unittest
from calc import add, subtract


class ArithmeticTests(unittest.TestCase):
    def test_positive(self): self.assertEqual(add(2, 3), 5)
    def test_negative(self): self.assertEqual(add(-5, -7), -12)
    def test_zero(self): self.assertEqual(add(9, 0), 9)
    def test_subtraction_unchanged(self): self.assertEqual(subtract(10, 3), 7)
