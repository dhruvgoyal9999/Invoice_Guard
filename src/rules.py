"""
The rule set. Spec Section 10.

One function per rule. Every rule returns a RuleResult with the same shape.

THREE INVARIANTS
  1. Every rule runs on every invoice. Never short-circuit. Even after a
     blocker fails, keep going -- a reviewer wants the whole picture, not the
     first thing that broke.
  2. A rule that cannot run returns SKIP with a reason. SKIP IS NEVER PASS.
     A check that could not run must never read as a check that succeeded.
  3. Rules do not know about decisions. Each states its own severity; decide.py
     resolves the collection into one of four outcomes.

FAMILIES
  R-0xx completeness and integrity     R-1xx matching        R-2xx vendor
  R-3xx financial                      R-4xx tax
  R-5xx duplicates                     R-6xx dates
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from . import config
from .money import OverageEvaluation, expected_tax_paise, format_paise, within_rounding
from .schemas import (
    ExtractedInvoice,
    ExtractionStage,
    MatchStatus,
    MatchingResult,
    PurchaseOrder,
    RuleResult,
    RuleStatus,
    Severity,
    Vendor,
)


# ---------------------------------------------------------------------------
# CONTEXT
# ---------------------------------------------------------------------------

@dataclass
class RuleContext:
    """
    Everything the rules are allowed to see.

    Typed rather than a loose dict so a rule reads as ctx.po.status instead of
    ctx["po"]["status"], and so a missing piece fails here at construction
    rather than inside rule twenty-three.

    Fields that may legitimately be absent are Optional. A rule that needs an
    absent one returns SKIP, never PASS.
    """

    extraction: ExtractionStage
    matching: MatchingResult
    po: PurchaseOrder | None = None
    vendor: Vendor | None = None
    overage: OverageEvaluation | None = None
    prior_invoices: list[dict] = field(default_factory=list)
    today: date = field(default_factory=date.today)

    @property
    def invoice(self) -> ExtractedInvoice:
        return self.extraction.fields


# ---------------------------------------------------------------------------
# PREREQUISITES
# ---------------------------------------------------------------------------

class Needs:
    """
    What a rule requires before it can meaningfully run.

    If a prerequisite is absent the rule returns SKIP, not PASS and not FAIL.
    This matters most for EXTRACTION: when the document could not be read at
    all, it would be dishonest to fail "vendor not identifiable" as a blocker.
    We never looked. Cascading blockers would turn an unreadable scan into a
    REJECT when the truthful outcome is "escalate this to a human".
    """

    EXTRACTION = "extraction"   # the quality gate passed
    PO = "po"                   # a purchase order was matched
    VENDOR = "vendor"           # the vendor was identified
    OVERAGE = "overage"         # the financial comparison could be computed


_SKIP_REASON = {
    Needs.EXTRACTION: "the document could not be read (see R-010)",
    Needs.PO: "no purchase order was matched",
    Needs.VENDOR: "the vendor could not be identified",
    Needs.OVERAGE: "the financial comparison could not be computed",
}


def _unmet(ctx: "RuleContext", needs: tuple[str, ...]) -> str | None:
    for need in needs:
        if need == Needs.EXTRACTION:
            q = ctx.extraction.quality
            if q is not None and not q.passes_gate:
                return _SKIP_REASON[need]
        elif getattr(ctx, need, None) is None:
            return _SKIP_REASON[need]
    return None


# ---------------------------------------------------------------------------
# VERDICTS
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """What a rule concluded. The decorator supplies id, name and severity."""

    status: RuleStatus
    message: str
    expected: Any = None
    actual: Any = None

    @staticmethod
    def ok(message: str, expected: Any = None, actual: Any = None) -> "Verdict":
        return Verdict(RuleStatus.PASS, message, expected, actual)

    @staticmethod
    def no(message: str, expected: Any = None, actual: Any = None) -> "Verdict":
        return Verdict(RuleStatus.FAIL, message, expected, actual)

    @staticmethod
    def na(reason: str) -> "Verdict":
        """Could not be checked. NEVER equivalent to a pass."""
        return Verdict(RuleStatus.SKIP, f"Not checked: {reason}")


# ---------------------------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------------------------

RULES: list[Callable[["RuleContext"], RuleResult]] = []
_REGISTERED_IDS: set[str] = set()


def rule(rule_id: str, name: str, severity: Severity, *needs: str):
    """
    Register a rule and bind its metadata.

    Declaring id, name, severity and prerequisites here rather than inside the
    body means each appears exactly once, the runner cannot miss a rule, and
    duplicate ids are caught at import time.
    """
    if rule_id in _REGISTERED_IDS:
        raise ValueError(f"Duplicate rule id: {rule_id}")
    _REGISTERED_IDS.add(rule_id)

    def decorate(fn: Callable[["RuleContext"], Verdict]):
        def wrapped(ctx: "RuleContext") -> RuleResult:
            unmet = _unmet(ctx, needs)
            verdict = Verdict.na(unmet) if unmet else fn(ctx)
            return RuleResult(
                rule_id=rule_id, name=name, status=verdict.status,
                severity=severity, expected=verdict.expected,
                actual=verdict.actual, message=verdict.message,
            )

        wrapped.rule_id = rule_id
        wrapped.rule_name = name
        wrapped.severity = severity
        wrapped.needs = needs
        wrapped.__name__ = fn.__name__
        RULES.append(wrapped)
        return wrapped

    return decorate


# ===========================================================================
# R-0xx  COMPLETENESS AND INTEGRITY
# ===========================================================================

@rule("R-001", "Invoice number present", Severity.CRITICAL, Needs.EXTRACTION)
def r_001(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.invoice_number
    if f.is_present:
        return Verdict.ok(f"Invoice number {f.value} found.", "not null", f.value)
    return Verdict.no(
        f"No invoice number could be read. {f.reason or 'Not found on the document.'} "
        f"Without it this invoice cannot be checked for duplicates.",
        "not null", None,
    )


@rule("R-002", "Invoice date present and parseable", Severity.CRITICAL, Needs.EXTRACTION)
def r_002(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.invoice_date
    if not f.is_present:
        return Verdict.no(
            f"No invoice date. {f.reason or 'Not found on the document.'}",
            "a valid date", None,
        )
    try:
        datetime.strptime(f.value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return Verdict.no(f"Invoice date {f.value!r} is not a valid date.",
                          "YYYY-MM-DD", f.value)
    return Verdict.ok(f"Invoice dated {f.value}.", "a valid date", f.value)


@rule("R-003", "Subtotal present", Severity.CRITICAL, Needs.EXTRACTION)
def r_003(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.subtotal_paise
    if f.is_present:
        return Verdict.ok(f"Subtotal {format_paise(f.value)}.", "not null", f.value)
    return Verdict.no(
        f"No subtotal. {f.reason or 'Not found on the document.'} The subtotal "
        f"is what gets compared to the PO.",
        "not null", None,
    )


@rule("R-004", "Total present", Severity.CRITICAL, Needs.EXTRACTION)
def r_004(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.total_paise
    if f.is_present:
        return Verdict.ok(f"Total {format_paise(f.value)}.", "not null", f.value)
    return Verdict.no(f"No total. {f.reason or 'Not found on the document.'}",
                      "not null", None)


@rule("R-005", "Vendor identifiable", Severity.BLOCKER, Needs.EXTRACTION)
def r_005(ctx: RuleContext) -> Verdict:
    inv = ctx.invoice
    if inv.vendor_name.is_present or inv.vendor_gstin.is_present:
        found = inv.vendor_name.value or inv.vendor_gstin.value
        return Verdict.ok(f"Vendor identified from the document: {found}.",
                          "a name or GSTIN", found)
    return Verdict.no(
        "Neither a vendor name nor a GSTIN could be read. There is no way to "
        "establish who issued this invoice.",
        "a name or GSTIN", None,
    )


@rule("R-006", "Line items sum to subtotal", Severity.WARNING, Needs.EXTRACTION)
def r_006(ctx: RuleContext) -> Verdict:
    inv = ctx.invoice
    if not inv.line_items:
        return Verdict.na("no line items were extracted")
    if not inv.subtotal_paise.is_present:
        return Verdict.na("the subtotal could not be read")

    total = sum(li.amount_paise for li in inv.line_items)
    stated = inv.subtotal_paise.value
    if within_rounding(total, stated):
        return Verdict.ok(
            f"{len(inv.line_items)} line items sum to {format_paise(total)}, "
            f"matching the subtotal.",
            format_paise(stated), format_paise(total),
        )
    return Verdict.no(
        f"{len(inv.line_items)} line items sum to {format_paise(total)} but the "
        f"stated subtotal is {format_paise(stated)}. Likely a missed line rather "
        f"than a pricing error.",
        format_paise(stated), format_paise(total),
    )


@rule("R-007", "Subtotal plus tax equals total", Severity.CRITICAL, Needs.EXTRACTION)
def r_007(ctx: RuleContext) -> Verdict:
    inv = ctx.invoice
    if not (inv.subtotal_paise.is_present and inv.total_paise.is_present):
        return Verdict.na("subtotal or total could not be read")

    tax = inv.tax_paise.value or 0
    computed = inv.subtotal_paise.value + tax
    stated = inv.total_paise.value
    if within_rounding(computed, stated):
        return Verdict.ok(
            f"{format_paise(inv.subtotal_paise.value)} + {format_paise(tax)} "
            f"= {format_paise(stated)}.",
            format_paise(stated), format_paise(computed),
        )
    return Verdict.no(
        f"The invoice does not add up: {format_paise(inv.subtotal_paise.value)} + "
        f"{format_paise(tax)} = {format_paise(computed)}, but the stated total is "
        f"{format_paise(stated)}.",
        format_paise(stated), format_paise(computed),
    )


@rule("R-008", "Critical fields meet confidence floor", Severity.WARNING, Needs.EXTRACTION)
def r_008(ctx: RuleContext) -> Verdict:
    weak = [
        f"{n} ({getattr(ctx.invoice, n).confidence:.2f})"
        for n in config.CRITICAL_FIELDS
        if getattr(ctx.invoice, n).is_present
        and getattr(ctx.invoice, n).confidence < config.CONFIDENCE_CRITICAL
    ]
    if not weak:
        return Verdict.ok(
            f"All readable critical fields are at or above "
            f"{config.CONFIDENCE_CRITICAL:.2f} confidence.",
            f">= {config.CONFIDENCE_CRITICAL}", "all above floor",
        )
    return Verdict.no(
        f"Read with low confidence: {', '.join(weak)}. Worth a glance against "
        f"the source document.",
        f">= {config.CONFIDENCE_CRITICAL}", ", ".join(weak),
    )


@rule("R-009", "Supporting fields meet confidence floor", Severity.INFO, Needs.EXTRACTION)
def r_009(ctx: RuleContext) -> Verdict:
    weak = [
        f"{n} ({getattr(ctx.invoice, n).confidence:.2f})"
        for n in config.SUPPORTING_FIELDS
        if getattr(ctx.invoice, n).is_present
        and getattr(ctx.invoice, n).confidence < config.CONFIDENCE_SUPPORTING
    ]
    if not weak:
        return Verdict.ok("Supporting fields are adequately confident.",
                          f">= {config.CONFIDENCE_SUPPORTING}", "all above floor")
    return Verdict.no(f"Lower-confidence supporting fields: {', '.join(weak)}.",
                      f">= {config.CONFIDENCE_SUPPORTING}", ", ".join(weak))


@rule("R-010", "Extraction quality sufficient", Severity.CRITICAL)
def r_010(ctx: RuleContext) -> Verdict:
    """
    The quality gate from extract.py. The gate reports; this rule decides.

    Deliberately declares NO prerequisites -- it is the rule that reports the
    extraction failing, so it must still run when everything else skips.
    """
    q = ctx.extraction.quality
    if q is None:
        return Verdict.na("no quality assessment was produced")
    if q.passes_gate:
        return Verdict.ok(
            f"Extraction usable: {q.critical_present}/{q.critical_total} critical "
            f"fields at {q.mean_critical_confidence:.2f} mean confidence "
            f"({ctx.extraction.tier.value} tier).",
            "gate passed", "gate passed",
        )
    return Verdict.no(q.gate_reason or "Extraction quality insufficient.",
                      "gate passed", "gate failed")


# ===========================================================================
# R-1xx  MATCHING
# ===========================================================================

@rule("R-101", "A purchase order was matched", Severity.BLOCKER, Needs.EXTRACTION)
def r_101(ctx: RuleContext) -> Verdict:
    m = ctx.matching
    if m.match_status == MatchStatus.MATCHED and m.po_number:
        return Verdict.ok(
            f"Matched to {m.po_number} at layer {m.match_layer} "
            f"({m.match_confidence.value.lower()} confidence).",
            "a matched PO", m.po_number,
        )
    detail = m.notes[-1] if m.notes else "no reason recorded"
    if m.match_status == MatchStatus.AMBIGUOUS:
        return Verdict.no(f"No single PO could be settled on. {detail}",
                          "a matched PO", "ambiguous")
    return Verdict.no(f"No purchase order could be matched. {detail}",
                      "a matched PO", "no match")


@rule("R-102", "Match is unambiguous", Severity.CRITICAL, Needs.EXTRACTION)
def r_102(ctx: RuleContext) -> Verdict:
    m = ctx.matching
    if m.match_status == MatchStatus.NO_MATCH:
        return Verdict.na("no PO was matched at all")
    if m.match_status == MatchStatus.AMBIGUOUS:
        listed = ", ".join(f"{c.po_number} ({c.score})" for c in m.candidates_considered)
        return Verdict.no(
            f"{len(m.candidates_considered)} purchase orders fit this invoice: "
            f"{listed}. A human must choose.",
            "exactly one candidate", f"{len(m.candidates_considered)} candidates",
        )
    return Verdict.ok(f"One clear match: {m.po_number}.",
                      "exactly one candidate", m.po_number)


@rule("R-103", "Invoice vendor matches PO vendor", Severity.BLOCKER,
      Needs.EXTRACTION, Needs.PO, Needs.VENDOR)
def r_103(ctx: RuleContext) -> Verdict:
    if ctx.vendor.vendor_id == ctx.po.vendor_id:
        return Verdict.ok(
            f"{ctx.vendor.legal_name} is the vendor on {ctx.po.po_number}.",
            ctx.po.vendor_id, ctx.vendor.vendor_id,
        )
    return Verdict.no(
        f"The invoice is from {ctx.vendor.legal_name} ({ctx.vendor.vendor_id}) "
        f"but {ctx.po.po_number} was raised for {ctx.po.vendor_name} "
        f"({ctx.po.vendor_id}).",
        ctx.po.vendor_id, ctx.vendor.vendor_id,
    )


@rule("R-104", "Purchase order is open", Severity.BLOCKER, Needs.EXTRACTION, Needs.PO)
def r_104(ctx: RuleContext) -> Verdict:
    if ctx.po.is_open:
        return Verdict.ok(f"{ctx.po.po_number} is open.", "OPEN", ctx.po.status.value)
    return Verdict.no(
        f"{ctx.po.po_number} is {ctx.po.status.value.lower()}. Nothing further "
        f"can be billed against it.",
        "OPEN", ctx.po.status.value,
    )


@rule("R-105", "Match came from a printed PO reference", Severity.WARNING,
      Needs.EXTRACTION, Needs.PO)
def r_105(ctx: RuleContext) -> Verdict:
    m = ctx.matching
    if m.match_layer == 1 and m.match_confidence.value == "HIGH":
        return Verdict.ok(f"The invoice printed {m.po_number} explicitly.",
                          "explicit reference", "explicit reference")
    return Verdict.no(
        f"{m.po_number} was worked out rather than read directly (layer "
        f"{m.match_layer}, {m.match_confidence.value.lower()} confidence). "
        f"Probably right, but the system guessed.",
        "explicit reference", f"layer {m.match_layer}",
    )


# ===========================================================================
# R-2xx  VENDOR
# ===========================================================================

@rule("R-201", "Vendor is on the approved list", Severity.BLOCKER,
      Needs.EXTRACTION, Needs.VENDOR)
def r_201(ctx: RuleContext) -> Verdict:
    if ctx.vendor.is_approved:
        return Verdict.ok(f"{ctx.vendor.legal_name} is an approved vendor.", True, True)
    return Verdict.no(
        f"{ctx.vendor.legal_name} ({ctx.vendor.vendor_id}) is not on the approved "
        f"vendor list. No payment can be released regardless of how the invoice "
        f"itself looks.",
        True, False,
    )


@rule("R-202", "Invoice GSTIN matches vendor master", Severity.CRITICAL,
      Needs.EXTRACTION, Needs.VENDOR)
def r_202(ctx: RuleContext) -> Verdict:
    printed = ctx.invoice.vendor_gstin.value
    if not printed:
        return Verdict.na("no GSTIN was printed on the invoice")
    if not ctx.vendor.gstin:
        return Verdict.na("no GSTIN on record for this vendor")

    if printed.strip().upper() == ctx.vendor.gstin.strip().upper():
        return Verdict.ok(f"GSTIN {printed} matches the vendor master.",
                          ctx.vendor.gstin, printed)
    return Verdict.no(
        f"The invoice carries GSTIN {printed} but {ctx.vendor.legal_name} is "
        f"registered as {ctx.vendor.gstin}. A mismatched tax number is a common "
        f"sign of a redirected payment.",
        ctx.vendor.gstin, printed,
    )


@rule("R-203", "Vendor has prior invoice history", Severity.WARNING,
      Needs.EXTRACTION, Needs.VENDOR)
def r_203(ctx: RuleContext) -> Verdict:
    n = len(ctx.prior_invoices)
    if n:
        return Verdict.ok(
            f"{n} prior invoice(s) on record for {ctx.vendor.legal_name}.",
            ">= 1", n,
        )
    return Verdict.no(
        f"This is the first invoice on record from {ctx.vendor.legal_name}. "
        f"First payments to a new vendor are worth confirming.",
        ">= 1", 0,
    )


# ===========================================================================
# R-3xx  FINANCIAL
# ===========================================================================

@rule("R-301", "Currency is supported", Severity.BLOCKER, Needs.EXTRACTION, Needs.PO)
def r_301(ctx: RuleContext) -> Verdict:
    """
    Guards the single-currency assumption (A-01).

    Invoice currency is not extracted -- every amount is parsed as INR. So the
    real risk is a PO raised in something else: the comparison would then be
    numerically fine and economically meaningless. This rule catches that.
    """
    if ctx.po.currency.upper() == config.CURRENCY:
        return Verdict.ok(f"{ctx.po.po_number} is denominated in "
                          f"{ctx.po.currency}.", config.CURRENCY, ctx.po.currency)
    return Verdict.no(
        f"{ctx.po.po_number} is in {ctx.po.currency}, but this system only "
        f"handles {config.CURRENCY}. Multi-currency matching is out of scope "
        f"(Spec A-01) and comparing the amounts would be meaningless.",
        config.CURRENCY, ctx.po.currency,
    )


@rule("R-302", "Over-billing within tolerance", Severity.CRITICAL,
      Needs.EXTRACTION, Needs.PO, Needs.OVERAGE)
def r_302(ctx: RuleContext) -> Verdict:
    """
    The centrepiece. allowed = min(1.5% of PO total, Rs 10,000), measured
    against REMAINING BALANCE rather than the full PO -- which is what makes
    progressive billing work with no special-case code.
    """
    ov = ctx.overage

    if ov.is_under_billing:
        return Verdict.ok(
            f"{format_paise(ov.invoice_subtotal_paise)} against a remaining "
            f"balance of {format_paise(ov.remaining_balance_paise)}. No "
            f"over-billing.",
            f"<= {format_paise(ov.allowed_overage_paise)} overage",
            format_paise(ov.overage_paise),
        )

    which = {
        "absolute_cap": (
            f"the Rs {config.TOLERANCE_ABSOLUTE_CAP_PAISE // 100:,} cap; "
            f"{config.TOLERANCE_PERCENT_DISPLAY}% would have permitted "
            f"{format_paise(ov.percent_allowance_paise)}"
        ),
        "percentage": (
            f"{config.TOLERANCE_PERCENT_DISPLAY}% of the PO; below the "
            f"{format_paise(ov.cap_paise)} cap"
        ),
        "equal": "both limits, which coincide at this PO value",
    }[ov.binding_constraint]

    if not ov.is_breach:
        return Verdict.ok(
            f"Over-billed by {format_paise(ov.overage_paise)} against an "
            f"allowance of {format_paise(ov.allowed_overage_paise)} "
            f"({ov.tolerance_consumption_pct:.0f}% used). Limit set by {which}.",
            f"<= {format_paise(ov.allowed_overage_paise)}",
            format_paise(ov.overage_paise),
        )

    return Verdict.no(
        f"Over-billed by {format_paise(ov.overage_paise)}, which exceeds the "
        f"allowance of {format_paise(ov.allowed_overage_paise)}. Limit set by "
        f"{which}. Invoice {format_paise(ov.invoice_subtotal_paise)} against a "
        f"remaining balance of {format_paise(ov.remaining_balance_paise)} on "
        f"{ctx.po.po_number}.",
        f"<= {format_paise(ov.allowed_overage_paise)}",
        format_paise(ov.overage_paise),
    )


@rule("R-303", "Contract ceiling not breached", Severity.CRITICAL,
      Needs.EXTRACTION, Needs.PO, Needs.OVERAGE)
def r_303(ctx: RuleContext) -> Verdict:
    """
    Cross-check of R-302, computed from a different direction.

    R-302 asks "is THIS invoice too big for what is left?"
    R-303 asks "would accepting it push the CONTRACT TOTAL past its ceiling?"

    Algebraically these are the same question, so they should always agree.
    That is the point: computing it twice from different inputs means a bug in
    one is caught by the other, and a disagreement is reported loudly rather
    than quietly producing a wrong decision.
    """
    ov = ctx.overage
    cumulative = ctx.po.already_invoiced_paise + ov.invoice_subtotal_paise
    ceiling = ctx.po.po_total_paise + ov.allowed_overage_paise
    breached = cumulative > ceiling

    if breached != ov.is_breach:
        return Verdict.no(
            f"INTERNAL INCONSISTENCY: the cumulative check and the per-invoice "
            f"check disagree ({cumulative} vs ceiling {ceiling}, R-302 says "
            f"breach={ov.is_breach}). This is a bug, not a vendor problem.",
            "R-302 and R-303 to agree", "they disagree",
        )

    if breached:
        return Verdict.no(
            f"Accepting this would take total billing on {ctx.po.po_number} to "
            f"{format_paise(cumulative)}, past the ceiling of "
            f"{format_paise(ceiling)} (PO {format_paise(ctx.po.po_total_paise)} "
            f"plus {format_paise(ov.allowed_overage_paise)} tolerance).",
            f"<= {format_paise(ceiling)}", format_paise(cumulative),
        )

    return Verdict.ok(
        f"Total billing on {ctx.po.po_number} would reach "
        f"{format_paise(cumulative)} against a ceiling of "
        f"{format_paise(ceiling)}.",
        f"<= {format_paise(ceiling)}", format_paise(cumulative),
    )


@rule("R-304", "Tolerance not substantially consumed", Severity.WARNING,
      Needs.EXTRACTION, Needs.PO, Needs.OVERAGE)
def r_304(ctx: RuleContext) -> Verdict:
    """
    Financially fine, but audit-worthy. An exact match sails through; an
    invoice that ate most of the allowance gets noted without being blocked.
    """
    ov = ctx.overage
    limit = config.TOLERANCE_CONSUMPTION_FLAG * 100

    if ov.is_under_billing:
        return Verdict.ok("No tolerance consumed -- the invoice is within the "
                          "remaining balance.", f"<= {limit:.0f}%", "0%")

    if ov.tolerance_consumption_pct <= limit:
        return Verdict.ok(
            f"{ov.tolerance_consumption_pct:.0f}% of the tolerance allowance "
            f"used.", f"<= {limit:.0f}%", f"{ov.tolerance_consumption_pct:.0f}%",
        )

    if ov.tolerance_consumption_pct > 100:
        # R-302 has already failed here; this rule only adds the magnitude.
        return Verdict.no(
            f"The tolerance allowance on {ctx.po.po_number} is fully consumed "
            f"and exceeded -- {ov.tolerance_consumption_pct:.0f}% of it. See "
            f"R-302 for the breach itself.",
            f"<= {limit:.0f}%", f"{ov.tolerance_consumption_pct:.0f}%",
        )

    remaining_allow = ov.allowed_overage_paise - ov.overage_paise
    return Verdict.no(
        f"This invoice uses {ov.tolerance_consumption_pct:.0f}% of the tolerance "
        f"allowance on {ctx.po.po_number}, leaving "
        f"{format_paise(remaining_allow)}. Within limits, but worth recording.",
        f"<= {limit:.0f}%", f"{ov.tolerance_consumption_pct:.0f}%",
    )


@rule("R-305", "Billing position against the PO", Severity.INFO,
      Needs.EXTRACTION, Needs.PO, Needs.OVERAGE)
def r_305(ctx: RuleContext) -> Verdict:
    """
    An observation, not a test. Under-billing is never a problem -- partial
    delivery and final invoices coming in under budget are both normal.

    It exists so the trace can show the difference between "we checked the
    billing position and it was under" and "we never looked".
    """
    ov = ctx.overage
    if ov.is_under_billing:
        shortfall = -ov.overage_paise
        pct = (shortfall / ctx.po.po_total_paise * 100) if ctx.po.po_total_paise else 0
        return Verdict.ok(
            f"Under-billed by {format_paise(shortfall)} ({pct:.1f}% of the PO). "
            f"Normal for a partial delivery or a job that came in under budget. "
            f"No limit applies to under-billing.",
            "informational", format_paise(ov.overage_paise),
        )
    return Verdict.ok(
        f"Billed at or above the remaining balance: overage "
        f"{format_paise(ov.overage_paise)}.",
        "informational", format_paise(ov.overage_paise),
    )


# ===========================================================================
# R-4xx  TAX
# ===========================================================================

@rule("R-401", "GST rate is a valid slab", Severity.CRITICAL, Needs.EXTRACTION)
def r_401(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.gst_rate
    if not f.is_present:
        return Verdict.na("no GST rate could be read")
    if f.value in config.VALID_GST_SLABS:
        return Verdict.ok(f"GST at {f.value}% is a valid slab.",
                          config.VALID_GST_SLABS, f.value)
    return Verdict.no(
        f"GST is charged at {f.value}%, which is not an Indian GST slab. Valid "
        f"slabs are {', '.join(f'{s}%' for s in config.VALID_GST_SLABS)}.",
        config.VALID_GST_SLABS, f.value,
    )


@rule("R-402", "Tax arithmetic is correct", Severity.CRITICAL, Needs.EXTRACTION)
def r_402(ctx: RuleContext) -> Verdict:
    inv = ctx.invoice
    if not inv.subtotal_paise.is_present:
        return Verdict.na("the subtotal could not be read")
    if not inv.gst_rate.is_present:
        return Verdict.na("the GST rate could not be read")
    if not inv.tax_paise.is_present:
        return Verdict.na("the tax amount could not be read")

    expected = expected_tax_paise(inv.subtotal_paise.value, inv.gst_rate.value)
    actual = inv.tax_paise.value
    if within_rounding(expected, actual):
        return Verdict.ok(
            f"{format_paise(inv.subtotal_paise.value)} at {inv.gst_rate.value}% "
            f"gives {format_paise(actual)}.",
            format_paise(expected), format_paise(actual),
        )
    return Verdict.no(
        f"Tax does not follow the stated rate: "
        f"{format_paise(inv.subtotal_paise.value)} at {inv.gst_rate.value}% "
        f"should be {format_paise(expected)}, but "
        f"{format_paise(actual)} is charged.",
        format_paise(expected), format_paise(actual),
    )


@rule("R-403", "Vendor GSTIN is well formed", Severity.WARNING, Needs.EXTRACTION)
def r_403(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.vendor_gstin
    if not f.is_present:
        return Verdict.no(
            "No GSTIN printed on the invoice. A tax invoice should carry the "
            "supplier's registration number.",
            f"{config.GSTIN_LENGTH} characters", None,
        )
    value = f.value.strip()
    if len(value) == config.GSTIN_LENGTH:
        return Verdict.ok(f"GSTIN {value} is the correct length.",
                          f"{config.GSTIN_LENGTH} characters", len(value))
    return Verdict.no(
        f"GSTIN {value!r} is {len(value)} characters; a valid GSTIN is "
        f"{config.GSTIN_LENGTH}.",
        f"{config.GSTIN_LENGTH} characters", len(value),
    )


@rule("R-404", "GST rate matches the PO expectation", Severity.WARNING,
      Needs.EXTRACTION, Needs.PO)
def r_404(ctx: RuleContext) -> Verdict:
    f = ctx.invoice.gst_rate
    if not f.is_present:
        return Verdict.na("no GST rate could be read")
    if ctx.po.expected_gst_rate is None:
        return Verdict.na(f"no expected rate recorded on {ctx.po.po_number}")

    if f.value == ctx.po.expected_gst_rate:
        return Verdict.ok(
            f"GST at {f.value}% matches what {ctx.po.po_number} expects.",
            ctx.po.expected_gst_rate, f.value,
        )
    return Verdict.no(
        f"The invoice charges GST at {f.value}% but {ctx.po.po_number} was "
        f"raised expecting {ctx.po.expected_gst_rate}%. Either the goods were "
        f"classified differently or the rate is wrong.",
        ctx.po.expected_gst_rate, f.value,
    )


# ===========================================================================
# R-5xx  DUPLICATES
# ===========================================================================

def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _periods_overlap(a_from, a_to, b_from, b_to) -> bool | None:
    """True/False if both periods are known, None if either is missing."""
    af, at, bf, bt = map(_parse_iso, (a_from, a_to, b_from, b_to))
    if not (af and at and bf and bt):
        return None
    return af <= bt and bf <= at


@rule("R-501", "Not an exact duplicate", Severity.BLOCKER,
      Needs.EXTRACTION, Needs.VENDOR)
def r_501(ctx: RuleContext) -> Verdict:
    """
    Same vendor, same invoice number, already processed.

    SKIPS when the invoice number could not be read. That distinction is the
    whole of EC-4: a duplicate check that could not run must never report as a
    duplicate check that passed, or an unreadable invoice would look verified.
    """
    f = ctx.invoice.invoice_number
    if not f.is_present:
        return Verdict.na(
            "the invoice number could not be read, so duplicates cannot be "
            "checked at all"
        )

    key = f.value.strip().upper()
    for prior in ctx.prior_invoices:
        if str(prior.get("invoice_number") or "").strip().upper() == key:
            return Verdict.no(
                f"Invoice {f.value} from {ctx.vendor.legal_name} has already "
                f"been processed (dated {prior.get('invoice_date')}, "
                f"{format_paise(prior.get('subtotal_paise') or 0)}, outcome "
                f"{prior.get('decision')}).",
                "not previously seen", f.value,
            )
    return Verdict.ok(
        f"Invoice {f.value} has not been seen before from "
        f"{ctx.vendor.legal_name} ({len(ctx.prior_invoices)} prior on record).",
        "not previously seen", f.value,
    )


@rule("R-502", "Not a suspected near-duplicate", Severity.CRITICAL,
      Needs.EXTRACTION, Needs.VENDOR)
def r_502(ctx: RuleContext) -> Verdict:
    """
    EC-2. Same vendor, same amount, close in time, different invoice number.

    Every naive duplicate heuristic fires on legitimate recurring billing: a
    monthly retainer produces identical amounts a fortnight apart. Amount and
    date cannot separate the two cases. The distinguishing signal is the
    SERVICE PERIOD, which is why the extractor goes looking for it.

        periods present and non-overlapping -> legitimate, PASS
        periods present and overlapping     -> genuine suspicion, FAIL
        either period missing               -> cannot determine, FAIL
    """
    inv = ctx.invoice
    if not inv.subtotal_paise.is_present:
        return Verdict.na("the subtotal could not be read")

    this_date = _parse_iso(inv.invoice_date.value)
    if this_date is None:
        return Verdict.na("the invoice date could not be read")

    this_number = (inv.invoice_number.value or "").strip().upper()
    period = inv.service_period.value

    suspects = []
    for prior in ctx.prior_invoices:
        if (prior.get("subtotal_paise") or -1) != inv.subtotal_paise.value:
            continue
        if str(prior.get("invoice_number") or "").strip().upper() == this_number:
            continue  # that is R-501's job, not this one
        prior_date = _parse_iso(prior.get("invoice_date"))
        if prior_date is None:
            continue
        if abs((this_date - prior_date).days) > config.NEAR_DUPLICATE_WINDOW_DAYS:
            continue
        suspects.append(prior)

    if not suspects:
        return Verdict.ok(
            f"No other invoice from {ctx.vendor.legal_name} for "
            f"{format_paise(inv.subtotal_paise.value)} within "
            f"{config.NEAR_DUPLICATE_WINDOW_DAYS} days.",
            "no near-duplicate", "none found",
        )

    for prior in suspects:
        overlap = _periods_overlap(
            period.from_date if period else None,
            period.to_date if period else None,
            prior.get("service_period_from"),
            prior.get("service_period_to"),
        )
        gap = abs((this_date - _parse_iso(prior["invoice_date"])).days)

        if overlap is None:
            return Verdict.no(
                f"Possible duplicate of {prior.get('invoice_number')} "
                f"({format_paise(inv.subtotal_paise.value)}, {gap} days apart) "
                f"and it cannot be ruled out: no service period on one or both "
                f"invoices, so there is nothing to tell recurring billing from "
                f"a repeat submission.",
                "distinguishable", "service period missing",
            )
        if overlap:
            return Verdict.no(
                f"Duplicate suspected: {prior.get('invoice_number')} covers the "
                f"same service period ({prior.get('service_period_from')} to "
                f"{prior.get('service_period_to')}) for the same amount, "
                f"{gap} days earlier.",
                "distinct service periods", "overlapping periods",
            )

    others = ", ".join(str(s.get("invoice_number")) for s in suspects)
    return Verdict.ok(
        f"Matches {others} on amount and timing, but the service periods do not "
        f"overlap -- this is recurring billing, not a duplicate. This invoice "
        f"covers {period.from_date} to {period.to_date}.",
        "distinct service periods", "periods differ",
    )


# ===========================================================================
# R-6xx  DATES
# ===========================================================================

@rule("R-601", "Invoice is not dated before its PO", Severity.CRITICAL,
      Needs.EXTRACTION, Needs.PO)
def r_601(ctx: RuleContext) -> Verdict:
    """
    A genuine fraud signal. An invoice predating the PO that authorised it
    means either back-dating or a purchase made without approval.
    """
    inv_date = _parse_iso(ctx.invoice.invoice_date.value)
    if inv_date is None:
        return Verdict.na("the invoice date could not be read")

    if inv_date >= ctx.po.po_date:
        return Verdict.ok(
            f"Invoiced {inv_date}, {(inv_date - ctx.po.po_date).days} days after "
            f"{ctx.po.po_number} was raised.",
            f">= {ctx.po.po_date}", str(inv_date),
        )
    return Verdict.no(
        f"The invoice is dated {inv_date}, which is "
        f"{(ctx.po.po_date - inv_date).days} days BEFORE {ctx.po.po_number} was "
        f"raised on {ctx.po.po_date}. Either the invoice is back-dated or the "
        f"purchase was made without approval.",
        f">= {ctx.po.po_date}", str(inv_date),
    )


@rule("R-602", "Invoice is not dated in the future", Severity.CRITICAL,
      Needs.EXTRACTION)
def r_602(ctx: RuleContext) -> Verdict:
    inv_date = _parse_iso(ctx.invoice.invoice_date.value)
    if inv_date is None:
        return Verdict.na("the invoice date could not be read")
    if inv_date <= ctx.today:
        return Verdict.ok(f"Invoiced {inv_date}.", f"<= {ctx.today}", str(inv_date))
    return Verdict.no(
        f"The invoice is dated {inv_date}, "
        f"{(inv_date - ctx.today).days} days in the future.",
        f"<= {ctx.today}", str(inv_date),
    )


@rule("R-603", "Invoice falls within the PO validity window", Severity.WARNING,
      Needs.EXTRACTION, Needs.PO)
def r_603(ctx: RuleContext) -> Verdict:
    if ctx.po.valid_until is None:
        return Verdict.na(f"no validity date recorded on {ctx.po.po_number}")
    inv_date = _parse_iso(ctx.invoice.invoice_date.value)
    if inv_date is None:
        return Verdict.na("the invoice date could not be read")

    if inv_date <= ctx.po.valid_until:
        return Verdict.ok(
            f"Invoiced {inv_date}, within {ctx.po.po_number}'s validity to "
            f"{ctx.po.valid_until}.",
            f"<= {ctx.po.valid_until}", str(inv_date),
        )
    return Verdict.no(
        f"{ctx.po.po_number} expired on {ctx.po.valid_until} but this invoice "
        f"is dated {inv_date}, {(inv_date - ctx.po.valid_until).days} days "
        f"later. Late billing against a valid PO is common, so this is noted "
        f"rather than blocked.",
        f"<= {ctx.po.valid_until}", str(inv_date),
    )


@rule("R-604", "Invoice date reading is unambiguous", Severity.INFO,
      Needs.EXTRACTION)
def r_604(ctx: RuleContext) -> Verdict:
    """
    Records when a date could have been read either way.

    Deviation from the spec, which set this at WARNING. Under A-08 every date
    is read DD/MM, and roughly a quarter of dates in any month have a day of 12
    or less -- so a warning here would flag a quarter of all invoices for
    following a documented convention correctly. That is noise, not signal.
    Recorded at INFO instead: visible in the trace, no effect on the decision.
    """
    inv_date = _parse_iso(ctx.invoice.invoice_date.value)
    if inv_date is None:
        return Verdict.na("the invoice date could not be read")

    if inv_date.day > 12:
        return Verdict.ok(
            f"{inv_date} can only be read one way -- day {inv_date.day} cannot "
            f"be a month.",
            "unambiguous", str(inv_date),
        )
    alternative = f"{inv_date.year}-{inv_date.day:02d}-{inv_date.month:02d}"
    return Verdict.ok(
        f"{inv_date} was read as DD/MM per the Indian convention (Spec A-08). "
        f"Read as MM/DD it would be {alternative}. No other evidence on the "
        f"document contradicts the DD/MM reading.",
        "unambiguous", f"DD/MM assumed; MM/DD would give {alternative}",
    )


# ===========================================================================
# RUNNER
# ===========================================================================

def run_all_rules(ctx: RuleContext) -> list[RuleResult]:
    """
    Execute every registered rule and return every result.

    NEVER short-circuits. Even after a blocker fails, the rest still run -- a
    reviewer wants the whole picture, not the first thing that broke. Results
    come back in rule-id order so the trace reads consistently.

    A rule that raises is reported as a FAIL naming itself as the fault, rather
    than taking down the batch. One broken rule should not stop an invoice from
    being assessed by the other thirty.
    """
    results: list[RuleResult] = []
    for fn in RULES:
        try:
            results.append(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            results.append(
                RuleResult(
                    rule_id=getattr(fn, "rule_id", "R-???"),
                    name=getattr(fn, "rule_name", fn.__name__),
                    status=RuleStatus.FAIL,
                    severity=Severity.CRITICAL,
                    expected="the rule to execute",
                    actual=f"{type(exc).__name__}: {exc}",
                    message=(
                        f"This rule crashed and could not assess the invoice: "
                        f"{type(exc).__name__}: {exc}. This is a system fault, "
                        f"not a vendor problem."
                    ),
                )
            )
    return sorted(results, key=lambda r: r.rule_id)


def rule_catalogue() -> list[dict]:
    """Every registered rule, for documentation and the UI."""
    return [
        {
            "rule_id": fn.rule_id,
            "name": fn.rule_name,
            "severity": fn.severity.value,
            "requires": list(fn.needs),
        }
        for fn in sorted(RULES, key=lambda f: f.rule_id)
    ]
