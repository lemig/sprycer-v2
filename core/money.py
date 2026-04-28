"""Money formatting that matches the legacy Sprycer export contract.

Locked in eng review 2C from the live exports.csv:
  - Whole euros render WITHOUT decimals: '€3' (NOT '€3.00')
  - Non-whole euros render with 2 decimals: '€3.04'
  - Thousands separator is comma per English locale: '€1,234.56'
    (untested in production sample; verify against legacy export of any
     Schleiper offer >= €1000 — TODO #1 in .context/todos.md)

Shared between the admin display and the H5 export so a single module
defines the byte contract that the cutover depends on.
"""
from __future__ import annotations


def format_euro(amount_cents: int | None) -> str:
    """Return '€3', '€3.04', or '€1,234.56' from an integer cent amount.

    None -> ''. Mirrors humanized_money_with_symbol legacy behavior.
    """
    if amount_cents is None:
        return ''
    amount = amount_cents / 100
    if amount == int(amount):
        return f'€{int(amount):,}'
    return f'€{amount:,.2f}'
