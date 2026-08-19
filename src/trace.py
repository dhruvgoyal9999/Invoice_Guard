"""
Audit trace assembly. Spec Section 13.

The trace is the product. The brief asks for "everything that happened in
between visible" -- this module is that sentence, implemented.

Five stages, in order, each showing what happened and why:
    1 extraction   what was read, how confidently, from which label
    2 matching     which PO, via which layer, and what else was considered
    3 financials   the tolerance arithmetic, including which limit bound
    4 rules        every rule that ran, with expected vs actual
    5 decision     the outcome and the specific rules that produced it
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .money import OverageEvaluation, format_paise
from .schemas import (
    DecisionResult,
    ExtractionStage,
    FinancialsStage,
    MatchingResult,
    RuleResult,
    RuleStatus,
    SourceType,
    Trace,
)

PIPELINE_VERSION = "1.0"


def financials_from_overage(ov: OverageEvaluation) -> FinancialsStage:
    """OverageEvaluation is a frozen dataclass; the trace wants a model."""
    return FinancialsStage(**ov.to_dict())


def build_trace(
    source_file: Path | str,
    extraction: ExtractionStage,
    matching: MatchingResult,
    rules: list[RuleResult],
    decision: DecisionResult,
    overage: OverageEvaluation | None = None,
) -> Trace:
    """Assemble one complete record."""
    path = Path(source_file)
    source_type = (
        SourceType.CLEAN_PDF if extraction.extractable_text
        else SourceType.SCANNED_IMAGE
    )

    # Trace ids lead with the invoice stem so outputs/traces/ is browsable by
    # eye. A bare uuid is technically sufficient and practically useless when
    # you are trying to find one invoice among twenty-one.
    trace_id = f"{path.stem}_{uuid.uuid4().hex[:8]}"

    return Trace(
        trace_id=trace_id,
        processed_at=datetime.now(timezone.utc),
        source_file=str(path),
        source_type=source_type,
        pipeline_version=PIPELINE_VERSION,
        stage_1_extraction=extraction,
        stage_2_matching=matching,
        stage_3_financials=financials_from_overage(overage) if overage else None,
        stage_4_rules=rules,
        stage_5_decision=decision,
    )


def write_trace(trace: Trace) -> Path:
    """Write to outputs/traces/<trace_id>.json and return the path."""
    config.TRACE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.TRACE_DIR / f"{trace.trace_id}.json"
    path.write_text(
        trace.model_dump_json(indent=2, exclude_none=False), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------

_MEANING = {
    "AUTO_APPROVE": "queued for payment with no human involvement",
    "APPROVE_WITH_FLAG": "cleared for payment, but recorded for audit",
    "HOLD_FOR_REVIEW": "held for an AP reviewer",
    "REJECT": "cannot be processed",
}


def template_summary(trace: Trace) -> str:
    """
    Build the summary deterministically. Always available, no API, no cost.

    This is the DEFAULT. The model version below is optional and only ever
    rephrases a decision that has already been made.
    """
    d = trace.stage_5_decision
    m = trace.stage_2_matching
    f = trace.stage_3_financials
    by_id = {r.rule_id: r for r in trace.stage_4_rules}

    lines: list[str] = []
    lines.append(
        f"{d.decision.value.replace('_', ' ').title()} -- "
        f"{_MEANING[d.decision.value]}."
    )

    if m.po_number:
        how = (
            "from the PO reference printed on the invoice"
            if m.match_layer == 1 and m.match_confidence.value == "HIGH"
            else f"by inference (layer {m.match_layer}, "
                 f"{m.match_confidence.value.lower()} confidence)"
        )
        lines.append(f"Matched to {m.po_number} {how}.")
    else:
        lines.append("No purchase order could be matched.")

    if f:
        if f.is_under_billing:
            lines.append(
                f"Billed {format_paise(f.invoice_subtotal_paise)} against a "
                f"remaining balance of {format_paise(f.remaining_balance_paise)}."
            )
        else:
            bound = {"absolute_cap": "the absolute cap",
                     "percentage": "the percentage limit",
                     "equal": "both limits"}[f.binding_constraint]
            lines.append(
                f"Billed {format_paise(f.invoice_subtotal_paise)} against a "
                f"remaining balance of {format_paise(f.remaining_balance_paise)}, "
                f"an overage of {format_paise(f.overage_paise)} against an "
                f"allowance of {format_paise(f.allowed_overage_paise)} set by "
                f"{bound} ({f.tolerance_consumption_pct:.0f}% used)."
            )

    if d.determined_by:
        lines.append("")
        lines.append("Decided by:")
        for rid in d.determined_by:
            lines.append(f"  {rid} - {by_id[rid].message}")
    else:
        lines.append("Every rule passed.")

    skipped = [r for r in trace.stage_4_rules if r.status == RuleStatus.SKIP]
    if skipped:
        lines.append("")
        lines.append(
            f"{len(skipped)} check(s) could not be run: "
            f"{', '.join(r.rule_id for r in skipped)}. These are recorded as "
            f"not checked, not as passed."
        )

    return "\n".join(lines)


def model_summary(trace: Trace) -> str | None:
    """
    Optional. Rephrases an already-made decision in plain prose.

    The ONLY place a model appears after extraction, and it cannot change any
    outcome -- it receives the finished decision and describes it. Returns None
    on any failure so the caller falls back to the template. The system must
    work with no API key at all.
    """
    if not getattr(config, "SUMMARY_USE_MODEL", False):
        return None
    try:
        import os

        from openai import OpenAI
        try:
            from dotenv import load_dotenv
            load_dotenv(config.ROOT_DIR / ".env")
        except ImportError:
            pass

        key = os.environ.get(config.OPENAI_API_KEY_ENV)
        if not key:
            return None

        facts = template_summary(trace)
        response = OpenAI(api_key=key).responses.create(
            model=config.OPENAI_VISION_MODEL,
            instructions=(
                "You write one short paragraph for an accounts payable "
                "reviewer. You are given a decision that has ALREADY been made "
                "by a deterministic rule engine. Describe it plainly. Do not "
                "second-guess it, do not add caveats, do not invent facts, and "
                "do not suggest a different outcome. Three sentences at most."
            ),
            input=facts,
            max_output_tokens=300,
        )
        text = (response.output_text or "").strip()
        return text or None
    except Exception:
        return None


def generate_summary(trace: Trace) -> str:
    """Model summary when enabled and available, template otherwise."""
    return model_summary(trace) or template_summary(trace)
