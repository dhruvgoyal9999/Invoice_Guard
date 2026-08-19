"""
Money handling and the tolerance engine.

Everything here is INTEGER PAISE. No float ever touches a monetary value.
Floats are produced only by format_paise() for display, and by the
consumption percentage, which is a ratio and not a money amount.

Reference: Spec Section 6.

NOTE — deviation from the spec repo layout (Section 16): the spec listed
config / schemas / store / extract / match / rules / decide / trace / pipeline.
This module was added because the tolerance calculation is used by rules.py,
trace.py and the UI, and duplicating it in three places is exactly the kind of
drift the spec warns about. Update Section 16 to include it.
"""

from dataclasses import dataclass, asdict
from typing import Literal

from . import config


# ---------------------------------------------------------------------------
# CONVERSION AND DISPLAY
# ---------------------------------------------------------------------------

def rupees_to_paise(rupees: float | int | str) -> int:
    """
    Convert a rupee amount to integer paise.

    Accepts a string to allow exact decimal input from CSVs, e.g. "2000.50".
    Rounds half-up at the paise boundary, which is what invoices do.
    """
    from decimal import Decimal, ROUND_HALF_UP

    value = Decimal(str(rupees))
    paise = (value * config.PAISE_PER_RUPEE).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(paise)


def paise_to_rupees(paise: int) -> float:
    """For display and reporting only. Never feed this back into a comparison."""
    return paise / config.PAISE_PER_RUPEE


def format_paise(paise: int) -> str:
    """
    Format paise as Indian-convention currency, e.g. 200000000 -> 'Rs 20,00,000.00'.

    Indian grouping: last three digits, then groups of two.
    """
    sign = "-" if paise < 0 else ""
    paise = abs(paise)

    whole, frac = divmod(paise, config.PAISE_PER_RUPEE)
    s = str(whole)

    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail

    return f"{sign}Rs {s}.{frac:02d}"


# ---------------------------------------------------------------------------
# TOLERANCE
# ---------------------------------------------------------------------------

BindingConstraint = Literal["percentage", "absolute_cap", "equal"]


@dataclass(frozen=True)
class ToleranceAllowance:
    """How much over-billing is permitted on a PO, and which limit produced it."""

    po_total_paise: int
    percent_allowance_paise: int
    cap_paise: int
    allowed_paise: int
    binding_constraint: BindingConstraint

    def explain(self) -> str:
        if self.binding_constraint == "absolute_cap":
            return (
                f"Allowance {format_paise(self.allowed_paise)} "
                f"(absolute cap; {config.TOLERANCE_PERCENT_DISPLAY}% would have "
                f"permitted {format_paise(self.percent_allowance_paise)})"
            )
        if self.binding_constraint == "percentage":
            return (
                f"Allowance {format_paise(self.allowed_paise)} "
                f"({config.TOLERANCE_PERCENT_DISPLAY}% of PO; below the "
                f"{format_paise(self.cap_paise)} cap)"
            )
        return (
            f"Allowance {format_paise(self.allowed_paise)} "
            f"(percentage and cap are equal at this PO value)"
        )


def allowed_overage(po_total_paise: int) -> ToleranceAllowance:
    """
    allowed = min( TOLERANCE_PERCENT% x po_total , TOLERANCE_ABSOLUTE_CAP )

    The percentage is always taken on the FULL PO total, never on the remaining
    balance. The allowance belongs to the contract, not to each invoice --
    otherwise a vendor could split into ten invoices and get ten allowances.
    """
    if po_total_paise < 0:
        raise ValueError("PO total cannot be negative")

    # Integer arithmetic throughout. // floors, so no float ever appears.
    percent_allowance = (
        po_total_paise * config.TOLERANCE_PERCENT_NUMERATOR
    ) // config.TOLERANCE_PERCENT_DENOMINATOR

    cap = config.TOLERANCE_ABSOLUTE_CAP_PAISE

    if percent_allowance < cap:
        binding: BindingConstraint = "percentage"
    elif percent_allowance > cap:
        binding = "absolute_cap"
    else:
        binding = "equal"

    return ToleranceAllowance(
        po_total_paise=po_total_paise,
        percent_allowance_paise=percent_allowance,
        cap_paise=cap,
        allowed_paise=min(percent_allowance, cap),
        binding_constraint=binding,
    )


@dataclass(frozen=True)
class OverageEvaluation:
    """The complete financial picture for one invoice against one PO."""

    po_total_paise: int
    already_invoiced_paise: int
    remaining_balance_paise: int
    invoice_subtotal_paise: int
    overage_paise: int            # negative or zero means under-billing
    allowed_overage_paise: int
    binding_constraint: BindingConstraint
    percent_allowance_paise: int
    cap_paise: int
    is_breach: bool
    is_under_billing: bool
    tolerance_consumption_pct: float  # 0.0 when under-billing

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_overage(
    po_total_paise: int,
    already_invoiced_paise: int,
    invoice_subtotal_paise: int,
) -> OverageEvaluation:
    """
    The core financial calculation. Spec Section 6.1.

        remaining = po_total - already_invoiced
        overage   = invoice_subtotal - remaining
        breach    = overage > allowed_overage      (strictly greater than)

    Tolerance is measured against REMAINING BALANCE, not the full PO. This is
    what makes progressive billing (EC-1) work without any special-case code.

    An invoice landing exactly on the limit PASSES. Using '>' rather than '>='
    is deliberate and is covered by a boundary test.
    """
    if invoice_subtotal_paise < 0:
        # Guard for Spec Q-08. Credit notes are out of scope for v1, but a
        # negative subtotal must not be allowed to flow silently into the
        # tolerance logic where it would read as extreme under-billing.
        raise ValueError(
            "Negative invoice subtotal (possible credit note) is not supported "
            "in v1 -- see Spec Q-08. Route to manual review."
        )

    allowance = allowed_overage(po_total_paise)

    remaining = po_total_paise - already_invoiced_paise
    overage = invoice_subtotal_paise - remaining

    is_breach = overage > allowance.allowed_paise
    is_under = overage <= 0

    if is_under or allowance.allowed_paise == 0:
        consumption = 0.0 if is_under else float("inf")
    else:
        consumption = overage / allowance.allowed_paise

    return OverageEvaluation(
        po_total_paise=po_total_paise,
        already_invoiced_paise=already_invoiced_paise,
        remaining_balance_paise=remaining,
        invoice_subtotal_paise=invoice_subtotal_paise,
        overage_paise=overage,
        allowed_overage_paise=allowance.allowed_paise,
        binding_constraint=allowance.binding_constraint,
        percent_allowance_paise=allowance.percent_allowance_paise,
        cap_paise=allowance.cap_paise,
        is_breach=is_breach,
        is_under_billing=is_under,
        tolerance_consumption_pct=round(consumption * 100, 2),
    )


# ---------------------------------------------------------------------------
# ARITHMETIC COMPARISON
# ---------------------------------------------------------------------------

def within_rounding(a: int, b: int) -> bool:
    """True if two paise amounts agree within the configured rounding tolerance."""
    return abs(a - b) <= config.ROUNDING_TOLERANCE_PAISE


def expected_tax_paise(subtotal_paise: int, gst_rate: int) -> int:
    """Expected tax for a subtotal at a given GST slab, in integer paise."""
    return (subtotal_paise * gst_rate) // 100
