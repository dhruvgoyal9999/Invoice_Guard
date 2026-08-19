"""
Tolerance engine tests -- the worked examples from Spec Section 6.3.

These are the tests that matter most in the whole project. If the tolerance
maths is wrong, every decision downstream is wrong and the audit trail is
confidently misleading.
"""

import pytest

from src import config
from src.money import (
    allowed_overage,
    evaluate_overage,
    expected_tax_paise,
    format_paise,
    rupees_to_paise,
    within_rounding,
)


def R(rupees) -> int:
    """Shorthand: rupees -> paise."""
    return rupees_to_paise(rupees)


# ---------------------------------------------------------------------------
# CONVERSION AND DISPLAY
# ---------------------------------------------------------------------------

def test_rupees_to_paise_is_exact():
    assert R(1) == 100
    assert R("2000.50") == 200_050
    assert R(0) == 0


def test_rupees_to_paise_rounds_half_up():
    assert R("0.005") == 1  # half-up, not banker's rounding


def test_indian_number_formatting():
    assert format_paise(R(2_00_000)) == "Rs 2,00,000.00"
    assert format_paise(R(20_00_000)) == "Rs 20,00,000.00"
    assert format_paise(R(1_00_00_000)) == "Rs 1,00,00,000.00"
    assert format_paise(R(500)) == "Rs 500.00"
    assert format_paise(R("1234.56")) == "Rs 1,234.56"


# ---------------------------------------------------------------------------
# ALLOWANCE -- which limit binds  (Spec Section 6.2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "po_rupees,expected_allowance_rupees,expected_binding",
    [
        (50_000,     750,    "percentage"),
        (2_00_000,   3_000,  "percentage"),
        (10_00_000,  10_000, "absolute_cap"),
        (50_00_000,  10_000, "absolute_cap"),
    ],
)
def test_allowance_table_from_spec(po_rupees, expected_allowance_rupees, expected_binding):
    a = allowed_overage(R(po_rupees))
    assert a.allowed_paise == R(expected_allowance_rupees)
    assert a.binding_constraint == expected_binding


def test_crossover_point_is_where_spec_says_it_is():
    """The two limits are equal at PO = Rs 6,66,667 (Spec Section 6.2)."""
    assert config.TOLERANCE_CROSSOVER_PAISE == R(6_66_666) + 66  # 666666.66

    just_below = allowed_overage(R(6_00_000))
    assert just_below.binding_constraint == "percentage"
    assert just_below.allowed_paise == R(9_000)

    just_above = allowed_overage(R(7_00_000))
    assert just_above.binding_constraint == "absolute_cap"
    assert just_above.allowed_paise == R(10_000)


def test_percentage_is_taken_on_full_po_not_remaining():
    """
    The allowance belongs to the contract, not to each invoice. A vendor must
    not be able to split into ten invoices and collect ten allowances.
    """
    a = allowed_overage(R(2_00_000))
    assert a.percent_allowance_paise == R(3_000)

    # Same PO, half already billed -- the allowance is unchanged.
    ev = evaluate_overage(R(2_00_000), R(1_00_000), R(1_00_000))
    assert ev.allowed_overage_paise == R(3_000)


def test_no_float_leaks_into_allowance():
    a = allowed_overage(R(3_33_333))
    assert isinstance(a.allowed_paise, int)
    assert isinstance(a.percent_allowance_paise, int)


# ---------------------------------------------------------------------------
# SPEC SECTION 6.3 -- WORKED EXAMPLES
# ---------------------------------------------------------------------------

def test_example_1_comfortable_pass():
    ev = evaluate_overage(R(2_00_000), 0, R(2_00_900))
    assert ev.remaining_balance_paise == R(2_00_000)
    assert ev.allowed_overage_paise == R(3_000)
    assert ev.overage_paise == R(900)
    assert ev.is_breach is False
    assert ev.tolerance_consumption_pct == 30.0
    assert ev.tolerance_consumption_pct <= config.TOLERANCE_CONSUMPTION_FLAG * 100


def test_example_2_passes_but_eats_most_of_allowance():
    ev = evaluate_overage(R(2_00_000), 0, R(2_02_600))
    assert ev.overage_paise == R(2_600)
    assert ev.is_breach is False
    assert ev.tolerance_consumption_pct == pytest.approx(86.67, abs=0.01)
    assert ev.tolerance_consumption_pct > config.TOLERANCE_CONSUMPTION_FLAG * 100


def test_example_3_cap_binds_where_percentage_would_have_passed():
    """
    EC-3. Overage is 1.25% of the PO -- under the 1.5% threshold -- but over
    the Rs 10,000 absolute cap. A percentage-only implementation approves this
    wrongly.
    """
    ev = evaluate_overage(R(20_00_000), 0, R(20_25_000))
    assert ev.overage_paise == R(25_000)
    assert ev.allowed_overage_paise == R(10_000)
    assert ev.percent_allowance_paise == R(30_000)
    assert ev.binding_constraint == "absolute_cap"
    assert ev.is_breach is True

    # Proof the percentage alone would not have caught it:
    assert ev.overage_paise < ev.percent_allowance_paise


def test_example_4_progressive_billing_lands_exactly_on_boundary():
    """
    EC-1. Three invoices against one PO. Tolerance measured against remaining
    balance, so this needs no special-case code.
    """
    po = R(10_00_000)

    a = evaluate_overage(po, 0, R(4_00_000))
    assert a.is_breach is False
    assert a.is_under_billing is True

    b = evaluate_overage(po, R(4_00_000), R(3_50_000))
    assert b.remaining_balance_paise == R(6_00_000)
    assert b.is_breach is False
    assert b.is_under_billing is True

    c = evaluate_overage(po, R(7_50_000), R(2_60_000))
    assert c.remaining_balance_paise == R(2_50_000)
    assert c.overage_paise == R(10_000)
    assert c.allowed_overage_paise == R(10_000)
    assert c.is_breach is False           # exactly on the line -> PASSES
    assert c.tolerance_consumption_pct == 100.0


def test_example_4_failure_twin():
    """Same setup, third invoice larger. Same logic, different outcome."""
    c = evaluate_overage(R(10_00_000), R(7_50_000), R(2_75_000))
    assert c.overage_paise == R(25_000)
    assert c.is_breach is True


# ---------------------------------------------------------------------------
# BOUNDARY BEHAVIOUR
# ---------------------------------------------------------------------------

def test_exactly_on_limit_passes_strictly_greater_than():
    """Spec 6.1: use '>', not '>='. One paise decides it."""
    on_limit = evaluate_overage(R(2_00_000), 0, R(2_03_000))
    assert on_limit.overage_paise == R(3_000)
    assert on_limit.is_breach is False

    one_paise_over = evaluate_overage(R(2_00_000), 0, R(2_03_000) + 1)
    assert one_paise_over.is_breach is True


def test_under_billing_never_breaches():
    ev = evaluate_overage(R(10_00_000), 0, R(1_000))
    assert ev.is_under_billing is True
    assert ev.is_breach is False
    assert ev.overage_paise < 0
    assert ev.tolerance_consumption_pct == 0.0


def test_exact_match_is_clean():
    ev = evaluate_overage(R(5_00_000), 0, R(5_00_000))
    assert ev.overage_paise == 0
    assert ev.is_breach is False
    assert ev.is_under_billing is True
    assert ev.tolerance_consumption_pct == 0.0


def test_negative_subtotal_is_rejected_not_processed():
    """Spec Q-08: a credit note must not read as extreme under-billing."""
    with pytest.raises(ValueError, match="Q-08"):
        evaluate_overage(R(1_00_000), 0, R(-5_000))


# ---------------------------------------------------------------------------
# ARITHMETIC HELPERS
# ---------------------------------------------------------------------------

def test_rounding_tolerance_is_one_rupee():
    assert within_rounding(R(1000), R(1000)) is True
    assert within_rounding(R(1000), R("1000.99")) is True
    assert within_rounding(R(1000), R("1001.00")) is True
    assert within_rounding(R(1000), R("1001.01")) is False


def test_expected_tax_at_each_slab():
    subtotal = R(1_00_000)
    assert expected_tax_paise(subtotal, 0) == 0
    assert expected_tax_paise(subtotal, 5) == R(5_000)
    assert expected_tax_paise(subtotal, 12) == R(12_000)
    assert expected_tax_paise(subtotal, 18) == R(18_000)
    assert expected_tax_paise(subtotal, 28) == R(28_000)


def test_tax_arithmetic_matches_spec_schema_example():
    """The line item worked through in Spec Section 7.3."""
    subtotal = R(42_000)
    tax = expected_tax_paise(subtotal, 18)
    assert tax == R(7_560)
    assert subtotal + tax == R(49_560)
