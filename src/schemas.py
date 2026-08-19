"""
Data contracts for the whole pipeline.

Every stage reads and writes these shapes. If a field is not defined here, it
does not exist. Reference: Spec Sections 7, 9, 10, 13.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config


# ---------------------------------------------------------------------------
# ENUMS
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    APPROVE_WITH_FLAG = "APPROVE_WITH_FLAG"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    REJECT = "REJECT"


class Severity(str, Enum):
    """Drives the decision. Spec Section 10.1."""
    BLOCKER = "BLOCKER"     # -> REJECT
    CRITICAL = "CRITICAL"   # -> HOLD_FOR_REVIEW
    WARNING = "WARNING"     # -> APPROVE_WITH_FLAG
    INFO = "INFO"           # no effect on outcome


class RuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"   # could not run -- MUST never be read as PASS (Spec EC-4)


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


class MatchConfidence(str, Enum):
    HIGH = "HIGH"           # explicit PO reference on the invoice
    MEDIUM = "MEDIUM"       # inferred from vendor + amount + date
    AMBIGUOUS = "AMBIGUOUS"
    NONE = "NONE"


class POStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class SourceType(str, Enum):
    CLEAN_PDF = "clean_pdf"
    SCANNED_IMAGE = "scanned_image"


class Tier(str, Enum):
    """
    Which extractor was used. Both tiers produce the same shape -- only the
    quality differs, so every downstream stage is tier-agnostic.
    """
    FREE = "free"        # pure Python, born-digital PDFs only
    PREMIUM = "premium"  # vision model, handles scans


# ---------------------------------------------------------------------------
# EXTRACTED FIELDS
# ---------------------------------------------------------------------------

T = TypeVar("T")


class ExtractedField(BaseModel, Generic[T]):
    """
    Every extracted value carries its own confidence and provenance.

    A null value is a legitimate, expected answer -- it means the field was not
    found on the document. The model must never invent one. When value is None,
    `reason` explains why. Spec Section 8.2, point 3.
    """

    model_config = ConfigDict(extra="forbid")

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    found_as: str | None = Field(
        default=None,
        description="The literal label seen on the document, e.g. 'Invoice No.'",
    )
    reason: str | None = Field(
        default=None,
        description="Why the field is null. Required when value is None.",
    )

    @property
    def is_present(self) -> bool:
        return self.value is not None

    def meets(self, floor: float) -> bool:
        """True if present AND confident enough."""
        return self.is_present and self.confidence >= floor


class ServicePeriod(BaseModel):
    """
    The period an invoice covers. Needed to tell recurring billing apart from
    a genuine duplicate. Spec EC-2.
    """

    model_config = ConfigDict(extra="forbid")

    from_date: date
    to_date: date

    @field_validator("to_date")
    @classmethod
    def _to_after_from(cls, v: date, info) -> date:
        start = info.data.get("from_date")
        if start and v < start:
            raise ValueError("Service period end date is before its start date")
        return v

    def overlaps(self, other: "ServicePeriod") -> bool:
        return self.from_date <= other.to_date and other.from_date <= self.to_date


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    qty: float | None = None
    unit_price_paise: int | None = None
    amount_paise: int
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SelfChecks(BaseModel):
    """
    Arithmetic checks run in Python after extraction, never by the model.
    A failing check is a finding, not something to silently repair.
    Spec Section 8.3.
    """

    model_config = ConfigDict(extra="forbid")

    line_items_sum_to_subtotal: bool | None = None
    subtotal_plus_tax_equals_total: bool | None = None
    tax_matches_rate: bool | None = None
    date_parseable: bool | None = None
    gstin_well_formed: bool | None = None
    notes: list[str] = Field(default_factory=list)


class ExtractedInvoice(BaseModel):
    """The full result of reading one invoice document. Spec Section 7.3."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: ExtractedField[str] = Field(default_factory=ExtractedField)
    invoice_date: ExtractedField[str] = Field(default_factory=ExtractedField)
    vendor_name: ExtractedField[str] = Field(default_factory=ExtractedField)
    vendor_gstin: ExtractedField[str] = Field(default_factory=ExtractedField)
    po_reference: ExtractedField[str] = Field(default_factory=ExtractedField)
    service_period: ExtractedField[ServicePeriod] = Field(default_factory=ExtractedField)

    line_items: list[LineItem] = Field(default_factory=list)

    subtotal_paise: ExtractedField[int] = Field(default_factory=ExtractedField)
    gst_rate: ExtractedField[int] = Field(default_factory=ExtractedField)
    tax_paise: ExtractedField[int] = Field(default_factory=ExtractedField)
    total_paise: ExtractedField[int] = Field(default_factory=ExtractedField)

    extraction_notes: list[str] = Field(default_factory=list)

    def low_confidence_fields(self) -> list[str]:
        """Present-but-shaky fields, per the configured floors."""
        weak: list[str] = []
        for name in config.CRITICAL_FIELDS:
            f = getattr(self, name, None)
            if isinstance(f, ExtractedField) and f.is_present:
                if f.confidence < config.CONFIDENCE_CRITICAL:
                    weak.append(name)
        for name in config.SUPPORTING_FIELDS:
            f = getattr(self, name, None)
            if isinstance(f, ExtractedField) and f.is_present:
                if f.confidence < config.CONFIDENCE_SUPPORTING:
                    weak.append(name)
        return weak

    def missing_fields(self) -> list[str]:
        return [
            name
            for name in config.CRITICAL_FIELDS + config.SUPPORTING_FIELDS
            if isinstance(getattr(self, name, None), ExtractedField)
            and not getattr(self, name).is_present
        ]


# ---------------------------------------------------------------------------
# MASTER DATA
# ---------------------------------------------------------------------------

class PurchaseOrder(BaseModel):
    """Spec Section 7.1."""

    model_config = ConfigDict(extra="forbid")

    po_number: str
    vendor_id: str
    vendor_name: str
    po_date: date
    po_total_paise: int = Field(ge=0, description="PRE-TAX value. Spec A-03.")
    currency: str = config.CURRENCY
    already_invoiced_paise: int = Field(default=0, ge=0)
    status: POStatus = POStatus.OPEN
    expected_gst_rate: int | None = None
    description: str | None = None
    valid_until: date | None = None

    @property
    def remaining_balance_paise(self) -> int:
        return self.po_total_paise - self.already_invoiced_paise

    @property
    def is_open(self) -> bool:
        return self.status == POStatus.OPEN


class Vendor(BaseModel):
    """Spec Section 7.2."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: str
    legal_name: str
    aliases: list[str] = Field(default_factory=list)
    gstin: str | None = None
    is_approved: bool = False
    onboarded_date: date | None = None


# ---------------------------------------------------------------------------
# MATCHING
# ---------------------------------------------------------------------------

class POCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_number: str
    score: int = Field(ge=0, le=100)
    reason: str


class MatchingResult(BaseModel):
    """Spec Section 9.3."""

    model_config = ConfigDict(extra="forbid")

    match_status: MatchStatus
    match_layer: int | None = Field(default=None, ge=1, le=4)
    match_confidence: MatchConfidence = MatchConfidence.NONE
    po_number: str | None = None
    vendor_id: str | None = None
    vendor_name_matched: str | None = None
    candidates_considered: list[POCandidate] = Field(default_factory=list)
    vendor_match_score: int | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# RULES AND DECISION
# ---------------------------------------------------------------------------

class RuleResult(BaseModel):
    """
    Every rule returns this shape, and every rule runs on every invoice.
    Never short-circuit. Spec Section 10.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    name: str
    status: RuleStatus
    severity: Severity
    expected: Any = None
    actual: Any = None
    message: str = Field(description="Plain English, written for a human reader.")

    @property
    def is_failure(self) -> bool:
        return self.status == RuleStatus.FAIL


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Decision
    determined_by: list[str] = Field(
        default_factory=list,
        description="The specific rule IDs that produced this outcome.",
    )
    rules_run: int = 0
    rules_passed: int = 0
    rules_failed: int = 0
    rules_skipped: int = 0
    summary: str = ""


# ---------------------------------------------------------------------------
# TRACE
# ---------------------------------------------------------------------------

class ExtractionQuality(BaseModel):
    """
    Whether an extraction is strong enough to act on automatically.

    Computed by extract.py, read by rule R-010. The gate REPORTS; the rule
    DECIDES. Failing the gate routes to HOLD_FOR_REVIEW -- never to an
    automatic approval or rejection.
    """

    model_config = ConfigDict(extra="forbid")

    extractable_text: bool
    critical_present: int
    critical_total: int
    missing_critical: list[str] = Field(default_factory=list)
    mean_critical_confidence: float = 0.0
    passes_gate: bool = True
    gate_reason: str | None = None


class ExtractionStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Tier
    model: str
    duration_ms: int | None = None
    cached: bool = False
    extractable_text: bool = False
    fields: ExtractedInvoice
    quality: ExtractionQuality | None = None
    self_checks: SelfChecks = Field(default_factory=SelfChecks)
    low_confidence_fields: list[str] = Field(default_factory=list)


class FinancialsStage(BaseModel):
    """Mirrors money.OverageEvaluation. Spec Section 13."""

    model_config = ConfigDict(extra="forbid")

    po_total_paise: int
    already_invoiced_paise: int
    remaining_balance_paise: int
    invoice_subtotal_paise: int
    overage_paise: int
    allowed_overage_paise: int
    binding_constraint: str
    percent_allowance_paise: int
    cap_paise: int
    is_breach: bool
    is_under_billing: bool
    tolerance_consumption_pct: float


class Trace(BaseModel):
    """
    The complete record of one invoice's journey. This is the product.
    Spec Section 13.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    processed_at: datetime
    source_file: str
    source_type: SourceType
    pipeline_version: str = "1.0"

    stage_1_extraction: ExtractionStage
    stage_2_matching: MatchingResult
    stage_3_financials: FinancialsStage | None = None  # None when no PO matched
    stage_4_rules: list[RuleResult] = Field(default_factory=list)
    stage_5_decision: DecisionResult
