"""
Rule unit tests.

Rules are tested DIRECTLY with hand-built contexts, not through the full
pipeline. A rule that only breaks under a specific PO state should fail in
half a second with a clear name, not require running twenty-one invoices to
reproduce.
"""

from datetime import date

import pytest

from src import config
from src.money import evaluate_overage, rupees_to_paise as R
from src.rules import RULES, RuleContext, rule_catalogue, run_all_rules
from src.schemas import (
    ExtractedField, ExtractedInvoice, ExtractionQuality, ExtractionStage,
    MatchConfidence, MatchingResult, POStatus, PurchaseOrder, RuleStatus,
    ServicePeriod, Severity, Tier, Vendor,
)

TODAY = date(2026, 8, 10)


# ---------------------------------------------------------------------------
# BUILDERS
# ---------------------------------------------------------------------------

def invoice(**over) -> ExtractedInvoice:
    base = dict(
        invoice_number="INV-1", invoice_date="2026-07-14",
        vendor_name="Pinnacle Office Supplies Pvt Ltd",
        vendor_gstin="07AABCP3312M1ZF", po_reference="PO-1001",
        subtotal=R(198500), gst_rate=18, tax=R(35730), total=R(234230),
    )
    base.update(over)
    inv = ExtractedInvoice()
    for attr, key in [("invoice_number", "invoice_number"),
                      ("invoice_date", "invoice_date"),
                      ("vendor_name", "vendor_name"),
                      ("vendor_gstin", "vendor_gstin"),
                      ("po_reference", "po_reference")]:
        v = base[key]
        setattr(inv, attr, ExtractedField(value=v, confidence=0.95 if v else 0.0,
                                          reason=None if v else "not found"))
    for attr, key in [("subtotal_paise", "subtotal"), ("gst_rate", "gst_rate"),
                      ("tax_paise", "tax"), ("total_paise", "total")]:
        v = base[key]
        setattr(inv, attr, ExtractedField(value=v, confidence=0.95 if v is not None else 0.0,
                                          reason=None if v is not None else "not found"))
    if base.get("period"):
        inv.service_period = ExtractedField(
            value=ServicePeriod(from_date=base["period"][0], to_date=base["period"][1]),
            confidence=0.9)
    return inv


def stage(inv: ExtractedInvoice, tier=Tier.FREE, gate=True, reason=None) -> ExtractionStage:
    return ExtractionStage(
        tier=tier, model="test", fields=inv,
        quality=ExtractionQuality(
            extractable_text=gate, critical_present=5, critical_total=5,
            mean_critical_confidence=0.95, passes_gate=gate, gate_reason=reason),
    )


def po(**over) -> PurchaseOrder:
    base = dict(po_number="PO-1001", vendor_id="V-004",
                vendor_name="Pinnacle Office Supplies Private Limited",
                po_date=date(2026, 6, 10), po_total_paise=R(200000),
                already_invoiced_paise=0, status=POStatus.OPEN,
                expected_gst_rate=18, valid_until=date(2026, 12, 31))
    base.update(over)
    return PurchaseOrder(**base)


def vendor(**over) -> Vendor:
    base = dict(vendor_id="V-004", legal_name="Pinnacle Office Supplies Private Limited",
                aliases=["Pinnacle Office"], gstin="07AABCP3312M1ZF",
                is_approved=True, onboarded_date=date(2023, 6, 15))
    base.update(over)
    return Vendor(**base)


def matching(**over) -> MatchingResult:
    base = dict(match_status="MATCHED", match_layer=1,
                match_confidence=MatchConfidence.HIGH, po_number="PO-1001",
                vendor_id="V-004")
    base.update(over)
    return MatchingResult(**base)


# Sentinel so "not supplied" is distinguishable from an explicit None or [].
# Using `x or default` here would silently swap an empty list for the default
# and quietly weaken the tests that depend on it.
_UNSET = object()


def ctx(inv=_UNSET, p=_UNSET, v=_UNSET, m=_UNSET, prior=_UNSET, gate=True,
        tier=Tier.FREE, reason=None) -> RuleContext:
    inv = invoice() if inv is _UNSET else inv
    p = po() if p is _UNSET else p
    v = vendor() if v is _UNSET else v
    m = matching() if m is _UNSET else m
    prior = [{"invoice_number": "OLD-1"}] if prior is _UNSET else prior

    over = None
    if p is not None and inv.subtotal_paise.is_present:
        over = evaluate_overage(p.po_total_paise, p.already_invoiced_paise,
                                inv.subtotal_paise.value)
    return RuleContext(
        extraction=stage(inv, tier=tier, gate=gate, reason=reason),
        matching=m, po=p, vendor=v, overage=over,
        prior_invoices=prior, today=TODAY,
    )


def fire(rule_id: str, c: RuleContext):
    return next(r for r in run_all_rules(c) if r.rule_id == rule_id)


# ---------------------------------------------------------------------------
# FRAMEWORK
# ---------------------------------------------------------------------------

def test_every_rule_returns_a_result():
    results = run_all_rules(ctx())
    assert len(results) == len(RULES)
    assert len({r.rule_id for r in results}) == len(RULES)


def test_results_are_sorted_by_rule_id():
    ids = [r.rule_id for r in run_all_rules(ctx())]
    assert ids == sorted(ids)


def test_healthy_invoice_fails_nothing():
    fails = [r.rule_id for r in run_all_rules(ctx()) if r.status == RuleStatus.FAIL]
    assert fails == []


def test_catalogue_covers_every_severity():
    sevs = {r["severity"] for r in rule_catalogue()}
    assert sevs == {"BLOCKER", "CRITICAL", "WARNING", "INFO"}


# ---------------------------------------------------------------------------
# SKIP IS NOT PASS  -- the central invariant
# ---------------------------------------------------------------------------

def test_failed_gate_skips_everything_except_r010():
    results = run_all_rules(ctx(gate=False, reason="Scanned document"))
    by_id = {r.rule_id: r for r in results}
    assert by_id["R-010"].status == RuleStatus.FAIL
    others = [r for r in results if r.rule_id != "R-010"]
    assert all(r.status == RuleStatus.SKIP for r in others)


def test_failed_gate_never_produces_a_blocker():
    """An unreadable document must HOLD, never REJECT. We did not look."""
    results = run_all_rules(ctx(gate=False, reason="Scanned document"))
    blockers = [r for r in results
                if r.severity == Severity.BLOCKER and r.status == RuleStatus.FAIL]
    assert blockers == []


def test_missing_invoice_number_skips_duplicate_check():
    """EC-4. A check that could not run must never read as one that passed."""
    r = fire("R-501", ctx(inv=invoice(invoice_number=None)))
    assert r.status == RuleStatus.SKIP
    assert r.status != RuleStatus.PASS


def test_no_po_skips_po_rules():
    c = ctx(p=None, m=matching(match_status="NO_MATCH", po_number=None,
                               match_confidence=MatchConfidence.NONE, match_layer=None))
    for rid in ["R-103", "R-104", "R-105", "R-301", "R-302", "R-303", "R-601", "R-603"]:
        assert fire(rid, c).status == RuleStatus.SKIP, rid


# ---------------------------------------------------------------------------
# COMPLETENESS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rid,field", [
    ("R-001", "invoice_number"), ("R-002", "invoice_date"),
    ("R-003", "subtotal"), ("R-004", "total"),
])
def test_missing_required_field_fails(rid, field):
    assert fire(rid, ctx(inv=invoice(**{field: None}))).status == RuleStatus.FAIL


def test_unparseable_date_fails():
    assert fire("R-002", ctx(inv=invoice(invoice_date="not-a-date"))).status == RuleStatus.FAIL


def test_vendor_unidentifiable_is_a_blocker():
    r = fire("R-005", ctx(inv=invoice(vendor_name=None, vendor_gstin=None)))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.BLOCKER


def test_arithmetic_mismatch_fails():
    r = fire("R-007", ctx(inv=invoice(total=R(999999))))
    assert r.status == RuleStatus.FAIL


def test_rounding_tolerance_absorbs_one_rupee():
    assert fire("R-007", ctx(inv=invoice(total=R(234230) + R(1)))).status == RuleStatus.PASS
    assert fire("R-007", ctx(inv=invoice(total=R(234230) + R(1) + 1))).status == RuleStatus.FAIL


# ---------------------------------------------------------------------------
# MATCHING AND VENDOR
# ---------------------------------------------------------------------------

def test_closed_po_is_a_blocker():
    r = fire("R-104", ctx(p=po(status=POStatus.CLOSED)))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.BLOCKER


def test_unapproved_vendor_is_a_blocker():
    r = fire("R-201", ctx(v=vendor(is_approved=False)))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.BLOCKER


def test_vendor_mismatch_against_po():
    r = fire("R-103", ctx(p=po(vendor_id="V-999")))
    assert r.status == RuleStatus.FAIL


def test_inferred_match_is_flagged():
    r = fire("R-105", ctx(m=matching(match_layer=2, match_confidence=MatchConfidence.MEDIUM)))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.WARNING


def test_gstin_mismatch_is_critical():
    r = fire("R-202", ctx(inv=invoice(vendor_gstin="27AABCS1429B1ZX")))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.CRITICAL


def test_first_invoice_warns():
    assert fire("R-203", ctx(prior=[])).status == RuleStatus.FAIL


# ---------------------------------------------------------------------------
# FINANCIAL  -- the dual threshold
# ---------------------------------------------------------------------------

def test_cap_binds_where_percentage_would_have_passed():
    """EC-3. 1.25% of the PO is under 1.5%, but over the Rs 10,000 cap."""
    c = ctx(inv=invoice(subtotal=R(2025000), tax=None, total=None),
            p=po(po_total_paise=R(2000000)))
    r = fire("R-302", c)
    assert r.status == RuleStatus.FAIL
    assert c.overage.binding_constraint == "absolute_cap"
    assert c.overage.overage_paise < c.overage.percent_allowance_paise


def test_percentage_binds_on_a_small_po():
    c = ctx(inv=invoice(subtotal=R(130000), tax=None, total=None),
            p=po(po_total_paise=R(125000)))
    assert fire("R-302", c).status == RuleStatus.FAIL
    assert c.overage.binding_constraint == "percentage"
    assert c.overage.overage_paise < c.overage.cap_paise


def test_exactly_on_the_limit_passes():
    c = ctx(inv=invoice(subtotal=R(203000), tax=None, total=None))
    assert fire("R-302", c).status == RuleStatus.PASS
    c2 = ctx(inv=invoice(subtotal=R(203000) + 1, tax=None, total=None))
    assert fire("R-302", c2).status == RuleStatus.FAIL


def test_progressive_billing_uses_remaining_balance():
    """EC-1. Third tranche lands on exactly the allowance."""
    c = ctx(inv=invoice(subtotal=R(260000), tax=None, total=None),
            p=po(po_total_paise=R(1000000), already_invoiced_paise=R(750000)))
    assert c.overage.overage_paise == R(10000)
    assert fire("R-302", c).status == RuleStatus.PASS
    assert fire("R-304", c).status == RuleStatus.FAIL  # 100% consumed


def test_r302_and_r303_always_agree():
    for sub, total, billed in [(198500, 200000, 0), (203000, 200000, 0),
                               (260000, 1000000, 750000), (2025000, 2000000, 0)]:
        c = ctx(inv=invoice(subtotal=R(sub), tax=None, total=None),
                p=po(po_total_paise=R(total), already_invoiced_paise=R(billed)))
        assert fire("R-302", c).status == fire("R-303", c).status


def test_under_billing_never_fails():
    c = ctx(inv=invoice(subtotal=R(1000), tax=None, total=None))
    assert fire("R-302", c).status == RuleStatus.PASS
    assert fire("R-304", c).status == RuleStatus.PASS
    assert fire("R-305", c).status == RuleStatus.PASS


# ---------------------------------------------------------------------------
# TAX
# ---------------------------------------------------------------------------

def test_invalid_gst_slab_fails():
    assert fire("R-401", ctx(inv=invoice(gst_rate=17, tax=None))).status == RuleStatus.FAIL


@pytest.mark.parametrize("slab", config.VALID_GST_SLABS)
def test_every_valid_slab_passes(slab):
    sub = R(100000)
    c = ctx(inv=invoice(subtotal=sub, gst_rate=slab,
                        tax=sub * slab // 100, total=sub + sub * slab // 100),
            p=po(expected_gst_rate=slab))
    assert fire("R-401", c).status == RuleStatus.PASS
    assert fire("R-402", c).status == RuleStatus.PASS


def test_tax_not_matching_rate_fails():
    assert fire("R-402", ctx(inv=invoice(tax=R(1), total=R(198501)))).status == RuleStatus.FAIL


def test_gst_rate_differing_from_po_warns():
    r = fire("R-404", ctx(p=po(expected_gst_rate=28)))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.WARNING


def test_malformed_gstin_warns():
    assert fire("R-403", ctx(inv=invoice(vendor_gstin="TOO-SHORT"))).status == RuleStatus.FAIL


# ---------------------------------------------------------------------------
# DUPLICATES  -- EC-2
# ---------------------------------------------------------------------------

def _prior(number, amount, day, period=None):
    row = {"invoice_number": number, "subtotal_paise": R(amount),
           "invoice_date": f"2026-03-{day:02d}", "decision": "AUTO_APPROVE"}
    if period:
        row["service_period_from"], row["service_period_to"] = period
    return row


def test_exact_duplicate_is_a_blocker():
    c = ctx(prior=[_prior("INV-1", 198500, 1)])
    r = fire("R-501", c)
    assert r.status == RuleStatus.FAIL and r.severity == Severity.BLOCKER


def test_recurring_billing_is_not_a_duplicate():
    """Same vendor, same amount, 15 days apart -- but different periods."""
    c = ctx(inv=invoice(invoice_number="SL-9034", invoice_date="2026-03-31",
                        subtotal=R(45000), tax=None, total=None,
                        period=("2026-03-16", "2026-03-31")),
            prior=[_prior("SL-9012", 45000, 16, ("2026-03-01", "2026-03-15"))])
    assert fire("R-502", c).status == RuleStatus.PASS


def test_overlapping_period_is_a_duplicate():
    c = ctx(inv=invoice(invoice_number="SL-9047", invoice_date="2026-04-02",
                        subtotal=R(45000), tax=None, total=None,
                        period=("2026-03-16", "2026-03-31")),
            prior=[_prior("SL-9034", 45000, 31, ("2026-03-16", "2026-03-31"))])
    r = fire("R-502", c)
    assert r.status == RuleStatus.FAIL and r.severity == Severity.CRITICAL


def test_missing_period_cannot_rule_out_a_duplicate():
    c = ctx(inv=invoice(invoice_number="SL-9047", invoice_date="2026-03-20",
                        subtotal=R(45000), tax=None, total=None),
            prior=[_prior("SL-9034", 45000, 16)])
    r = fire("R-502", c)
    assert r.status == RuleStatus.FAIL
    assert "service period" in r.message.lower()


def test_same_amount_outside_the_window_is_ignored():
    c = ctx(inv=invoice(invoice_date="2026-07-14", subtotal=R(45000),
                        tax=None, total=None),
            prior=[_prior("OLD", 45000, 1)])
    assert fire("R-502", c).status == RuleStatus.PASS


# ---------------------------------------------------------------------------
# DATES
# ---------------------------------------------------------------------------

def test_invoice_predating_its_po_fails():
    r = fire("R-601", ctx(inv=invoice(invoice_date="2026-01-01")))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.CRITICAL


def test_future_dated_invoice_fails():
    assert fire("R-602", ctx(inv=invoice(invoice_date="2027-01-01"))).status == RuleStatus.FAIL


def test_expired_po_warns_but_does_not_block():
    r = fire("R-603", ctx(p=po(valid_until=date(2026, 6, 30))))
    assert r.status == RuleStatus.FAIL and r.severity == Severity.WARNING


def test_ambiguous_date_is_recorded_not_penalised():
    r = fire("R-604", ctx(inv=invoice(invoice_date="2026-07-08")))
    assert r.status == RuleStatus.PASS
    assert r.severity == Severity.INFO
    assert "DD/MM" in r.message
