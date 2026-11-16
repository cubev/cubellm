import unittest
from decimal import Decimal, Inexact, ROUND_FLOOR, localcontext
from fractions import Fraction
from typing import Final

from cube_control.amounts import exact_sum


class AmountTests(unittest.TestCase):
    def test_exact_addition_matches_rational_arithmetic_without_changing_the_callers_context(self) -> None:
        cases: Final = (
            (),
            (Decimal("-0E-1000"), Decimal(0)),
            (Decimal("1E1000"), Decimal("-1E1000"), Decimal("1E-1000")),
            (Decimal("-0.12345678901234567890123456789"), Decimal("0.00000000000000000000000000001")),
            tuple(Decimal(f"{index}E{index - 25}") for index in range(-12, 13)),
            (Decimal("99999999999999999999999999999.99999999999"), Decimal("0.00000000001")),
        )
        with localcontext(prec=3, rounding=ROUND_FLOOR, Emax=5, Emin=-5, clamp=1) as caller:
            caller.traps[Inexact] = True
            for values in cases:
                with self.subTest(values=values):
                    expected: Final = sum((Fraction(value) for value in values), Fraction(0))
                    self.assertEqual(Fraction(exact_sum(values)), expected)
                    self.assertEqual(exact_sum(reversed(values)), exact_sum(values))
            with self.assertRaises(Inexact):
                _ = Decimal(1) / Decimal(3)


if __name__ == "__main__":
    unittest.main()
