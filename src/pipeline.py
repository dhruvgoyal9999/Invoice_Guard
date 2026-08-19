"""
End-to-end orchestration. Spec Section 15, Phase 7.

    extract -> match -> financials -> rules -> decide -> trace

This file contains NO business logic. It only sequences the stages and moves
data between them. Every judgement lives in rules.py; every calculation lives
in money.py; every decision policy lives in decide.py.

If you find yourself writing an `if` about invoices here, it belongs in a rule.
"""

from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from . import store
from .decide import decide, is_accepted
from .extract import extract_invoice
from .match import match_invoice
from .money import evaluate_overage
from .rules import RuleContext, run_all_rules
from .schemas import Tier, Trace
from .trace import build_trace, generate_summary, write_trace


class StageReporter(Protocol):
    """Called as each stage completes: (number, title, one-line plain summary)."""

    def __call__(self, step: int, title: str, detail: str) -> None: ...


def _noop(step: int, title: str, detail: str) -> None:
    pass


def process_invoice(
    pdf_path: Path | str,
    tier: Tier = Tier.FREE,
    *,
    use_cache: bool = True,
    commit: bool = True,
    today: date | None = None,
    on_stage: Callable[[int, str, str], None] | None = None,
) -> Trace:
    """
    Run one invoice through all five stages and return its complete trace.

    `commit` controls whether accepted invoices consume PO budget and enter the
    duplicate history. Set it False to assess an invoice without side effects
    -- useful for a what-if in the UI, or for re-running a batch.

    `on_stage` receives a plain-English line as each stage finishes. The
    pipeline reports what it did; the caller decides how to display it. This
    keeps narration out of the UI's hands without putting presentation logic
    in here -- the strings describe the work, not how to render it.
    """
    report = on_stage or _noop
    pdf_path = Path(pdf_path)

    # 1. extract
    extraction = extract_invoice(pdf_path, tier, use_cache=use_cache)
    invoice = extraction.fields
    report(1, "Reading the invoice", _describe_extraction(extraction))

    # 2. match
    matching = match_invoice(invoice)
    po = store.get_po(matching.po_number) if matching.po_number else None
    vendor = store.get_vendor(matching.vendor_id) if matching.vendor_id else None
    report(2, "Finding the purchase order", _describe_matching(matching))

    # 3. financials
    overage = None
    if po is not None and invoice.subtotal_paise.is_present:
        overage = evaluate_overage(
            po.po_total_paise, po.already_invoiced_paise, invoice.subtotal_paise.value
        )
    report(3, "Comparing the amounts", _describe_financials(overage, po))

    prior = store.find_prior_invoices(vendor.vendor_id) if vendor else []

    # 4. rules
    results = run_all_rules(
        RuleContext(
            extraction=extraction, matching=matching, po=po, vendor=vendor,
            overage=overage, prior_invoices=prior, today=today or date.today(),
        )
    )
    report(4, "Running the checks", _describe_rules(results))

    # 5. decide
    outcome = decide(results)
    report(5, "Reaching a decision", _describe_decision(outcome))

    trace = build_trace(
        source_file=pdf_path, extraction=extraction, matching=matching,
        rules=results, decision=outcome, overage=overage,
    )
    trace.stage_5_decision.summary = generate_summary(trace)

    if commit:
        _commit(trace, po, vendor, invoice, outcome)

    return trace


def _commit(trace, po, vendor, invoice, outcome) -> None:
    """
    Record the consequences of a decision.

    Two separate concerns, deliberately not conflated:

    PO BUDGET is consumed only by accepted invoices. A held invoice must not
    eat budget it was never approved for, or a queue of pending reviews would
    silently exhaust a PO.

    DUPLICATE HISTORY records EVERY processed invoice regardless of outcome.
    A rejected duplicate still has to be on record -- otherwise the same
    invoice submitted a third time would look brand new.
    """
    subtotal = invoice.subtotal_paise.value

    if is_accepted(outcome.decision) and po is not None and subtotal:
        store.update_already_invoiced(po.po_number, subtotal)

    if vendor is not None:
        period = invoice.service_period.value
        store.record_processed_invoice(
            vendor_id=vendor.vendor_id,
            invoice_number=invoice.invoice_number.value,
            subtotal_paise=subtotal or 0,
            invoice_date=invoice.invoice_date.value or "1970-01-01",
            po_number=po.po_number if po else None,
            service_period=(
                {"from_date": period.from_date, "to_date": period.to_date}
                if period else None
            ),
            decision=outcome.decision.value,
        )


def process_and_save(pdf_path: Path | str, tier: Tier = Tier.FREE, **kw) -> tuple[Trace, Path]:
    """Process one invoice and write its trace to disk."""
    trace = process_invoice(pdf_path, tier, **kw)
    return trace, write_trace(trace)


# ---------------------------------------------------------------------------
# STAGE NARRATION
# ---------------------------------------------------------------------------
# One plain sentence per stage, describing what actually happened. Written for
# someone who has never seen the system, so no rule ids and no jargon.

def _describe_extraction(extraction) -> str:
    f = extraction.fields
    q = extraction.quality
    if q and not q.passes_gate:
        return "Could not read this document. " + (q.gate_reason or "")
    found = sum(
        1 for n in (
            "invoice_number", "invoice_date", "vendor_name", "vendor_gstin",
            "po_reference", "subtotal_paise", "gst_rate", "tax_paise",
            "total_paise",
        ) if getattr(f, n).is_present
    )
    who = f.vendor_name.value or "an unnamed supplier"
    num = f.invoice_number.value or "no invoice number"
    return (f"Read {found} fields from {who}. Invoice {num}"
            f"{', dated ' + f.invoice_date.value if f.invoice_date.is_present else ''}, "
            f"{len(f.line_items)} line item(s).")


def _describe_matching(matching) -> str:
    if matching.match_status.value == "MATCHED":
        how = ("the reference was printed on the invoice"
               if matching.match_layer == 1
               else "worked out from the supplier, amount and date")
        return f"Matched to {matching.po_number} - {how}."
    if matching.match_status.value == "AMBIGUOUS":
        n = len(matching.candidates_considered)
        return f"{n} purchase orders could fit. A person needs to choose."
    return "No purchase order matched. " + (matching.notes[-1] if matching.notes else "")


def _describe_financials(overage, po) -> str:
    if overage is None:
        return "Nothing to compare against - no purchase order was matched."
    from .money import format_paise as fmt
    if overage.is_under_billing:
        return (f"Billed {fmt(overage.invoice_subtotal_paise)} against "
                f"{fmt(overage.remaining_balance_paise)} still available on "
                f"{po.po_number}. Comfortably within budget.")
    verdict = "over the limit" if overage.is_breach else "within the allowance"
    return (f"Billed {fmt(overage.invoice_subtotal_paise)} against "
            f"{fmt(overage.remaining_balance_paise)} remaining - "
            f"{fmt(overage.overage_paise)} more than expected, which is "
            f"{verdict} of {fmt(overage.allowed_overage_paise)}.")


def _describe_rules(results) -> str:
    from .schemas import RuleStatus
    passed = sum(1 for r in results if r.status == RuleStatus.PASS)
    failed = sum(1 for r in results if r.status == RuleStatus.FAIL)
    skipped = sum(1 for r in results if r.status == RuleStatus.SKIP)
    parts = [f"{passed} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} could not be run")
    return f"Ran {len(results)} checks: " + ", ".join(parts) + "."


def _describe_decision(outcome) -> str:
    label = outcome.decision.value.replace("_", " ").lower()
    if not outcome.determined_by:
        return f"Every check passed, so this is {label}."
    return (f"{label.capitalize()}, because of "
            f"{len(outcome.determined_by)} failed check(s): "
            f"{', '.join(outcome.determined_by)}.")
