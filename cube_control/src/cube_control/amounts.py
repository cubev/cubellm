from collections.abc import Iterable
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
from typing import Final


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    nonzero: Final = tuple(value for value in values if value)
    if not nonzero:
        return Decimal(0)
    lowest_exponent: Final = min(int(value.as_tuple().exponent) for value in nonzero)
    highest_digit: Final = max(value.adjusted() for value in nonzero)
    precision: Final = max(28, highest_digit - lowest_exponent + len(str(len(nonzero))) + 1)
    with localcontext(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN, clamp=0):
        return sum(nonzero, Decimal(0))
