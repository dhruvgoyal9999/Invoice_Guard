"""
Extraction entry point. Spec Section 8.

Two tiers, one interface:

    FREE     src/extract_local.py   pure Python, born-digital PDFs only
    PREMIUM  src/extract_vision.py  OpenAI vision, handles anything

Both return the same ExtractedInvoice, so matching, rules, decision and trace
are all tier-agnostic. Only the quality of what comes out differs.

This module owns three things the tiers should not duplicate:
  - the cache, keyed by (file hash, tier)
  - detecting whether a PDF has embedded text
  - the quality gate that feeds rule R-010
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from . import config
from .schemas import (
    ExtractedInvoice,
    ExtractionQuality,
    ExtractionStage,
    Tier,
)


# ---------------------------------------------------------------------------
# TEXT DETECTION
# ---------------------------------------------------------------------------

MIN_TEXT_CHARS = 120  # below this a PDF is effectively an image


def has_extractable_text(pdf_path: Path) -> bool:
    """
    True if the PDF carries real embedded text.

    This one check is the free tier's entire scan detector. A born-digital PDF
    returns the exact characters that were written into it. A scanned document
    is a picture of text and returns nothing.

    The threshold guards against image-only PDFs that still carry a stray
    label or an empty text layer.
    """
    doc = pymupdf.open(pdf_path)
    try:
        chars = sum(len(page.get_text().strip()) for page in doc)
    finally:
        doc.close()
    return chars >= MIN_TEXT_CHARS


def page_count(pdf_path: Path) -> int:
    doc = pymupdf.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# CACHE
# ---------------------------------------------------------------------------

def file_hash(pdf_path: Path) -> str:
    """SHA-256 of the file's bytes. Content, not filename -- renaming a file
    must not invalidate its extraction, and editing it must."""
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cache_path(digest: str, tier: Tier) -> Path:
    return config.CACHE_DIR / f"{digest}_{tier.value}.json"


def cache_get(pdf_path: Path, tier: Tier) -> dict | None:
    """Return the cached envelope, or None on a miss or a stale schema."""
    if not config.EXTRACTION_CACHE_ENABLED:
        return None
    path = _cache_path(file_hash(pdf_path), tier)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if envelope.get("schema_version") != config.CACHE_SCHEMA_VERSION:
        return None  # shape changed; treat as a miss
    return envelope


def cache_put(
    pdf_path: Path,
    tier: Tier,
    invoice: ExtractedInvoice,
    model: str,
    extractable_text: bool,
    duration_ms: int,
) -> None:
    if not config.EXTRACTION_CACHE_ENABLED:
        return
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema_version": config.CACHE_SCHEMA_VERSION,
        "source_file": pdf_path.name,
        "tier": tier.value,
        "model": model,
        "extractable_text": extractable_text,
        "duration_ms": duration_ms,
        "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "invoice": invoice.model_dump(mode="json"),
    }
    path = _cache_path(file_hash(pdf_path), tier)
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")


def clear_cache(tier: Tier | None = None) -> int:
    """Delete cached extractions. Returns how many were removed."""
    if not config.CACHE_DIR.exists():
        return 0
    pattern = f"*_{tier.value}.json" if tier else "*.json"
    files = list(config.CACHE_DIR.glob(pattern))
    for f in files:
        f.unlink()
    return len(files)


# ---------------------------------------------------------------------------
# QUALITY GATE  (feeds rule R-010)
# ---------------------------------------------------------------------------

def assess_quality(
    invoice: ExtractedInvoice,
    tier: Tier,
    extractable_text: bool,
) -> ExtractionQuality:
    """
    Judge whether this extraction is strong enough to act on automatically.

    Reports only. Rule R-010 turns a failed gate into HOLD_FOR_REVIEW. Nothing
    here ever causes an approval or a rejection -- an unreadable document is a
    reason to ask a human, not a reason to refuse a vendor.
    """
    missing: list[str] = []
    confidences: list[float] = []

    for name in config.CRITICAL_FIELDS:
        field = getattr(invoice, name, None)
        if field is None:
            continue
        if field.is_present:
            confidences.append(field.confidence)
        else:
            missing.append(name)

    total = len(config.CRITICAL_FIELDS)
    present = total - len(missing)
    mean_conf = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

    passes = True
    reason = None

    if tier is Tier.FREE and not extractable_text:
        passes = False
        reason = (
            "Scanned or image-only document. The free extractor reads embedded "
            "text only and cannot process this invoice. Retry with premium "
            "extraction."
        )
    elif len(missing) > config.QUALITY_MAX_MISSING_CRITICAL:
        passes = False
        reason = (
            f"{len(missing)} of {total} critical fields could not be read "
            f"({', '.join(missing)}). Too little was extracted to decide on."
        )
    elif confidences and mean_conf < config.QUALITY_MIN_MEAN_CONFIDENCE:
        passes = False
        reason = (
            f"Mean confidence across critical fields is {mean_conf:.2f}, below "
            f"the {config.QUALITY_MIN_MEAN_CONFIDENCE:.2f} floor. The document "
            f"was read, but not reliably enough to act on."
        )

    return ExtractionQuality(
        extractable_text=extractable_text,
        critical_present=present,
        critical_total=total,
        missing_critical=missing,
        mean_critical_confidence=mean_conf,
        passes_gate=passes,
        gate_reason=reason,
    )


# ---------------------------------------------------------------------------
# PUBLIC INTERFACE
# ---------------------------------------------------------------------------

def extract_invoice(
    pdf_path: Path,
    tier: Tier = Tier.FREE,
    use_cache: bool = True,
) -> ExtractionStage:
    """
    Read one invoice and return the complete extraction stage.

    Dispatches on tier, caches the result, assesses quality. The tier modules
    themselves know nothing about caching or gating -- they only read a
    document and return fields.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Invoice not found: {pdf_path}")

    has_text = has_extractable_text(pdf_path)

    if use_cache:
        envelope = cache_get(pdf_path, tier)
        if envelope is not None:
            invoice = ExtractedInvoice.model_validate(envelope["invoice"])
            return ExtractionStage(
                tier=tier,
                model=envelope["model"],
                duration_ms=envelope.get("duration_ms"),
                cached=True,
                extractable_text=envelope["extractable_text"],
                fields=invoice,
                quality=assess_quality(invoice, tier, envelope["extractable_text"]),
                low_confidence_fields=invoice.low_confidence_fields(),
            )

    started = time.perf_counter()

    if tier is Tier.FREE:
        from . import extract_local
        invoice = extract_local.extract(pdf_path)
        model = config.LOCAL_EXTRACTOR_NAME
    elif tier is Tier.PREMIUM:
        from . import extract_vision
        invoice = extract_vision.extract(pdf_path)
        model = config.OPENAI_VISION_MODEL
    else:
        raise ValueError(f"Unknown tier: {tier}")

    duration_ms = int((time.perf_counter() - started) * 1000)

    if use_cache:
        cache_put(pdf_path, tier, invoice, model, has_text, duration_ms)

    return ExtractionStage(
        tier=tier,
        model=model,
        duration_ms=duration_ms,
        cached=False,
        extractable_text=has_text,
        fields=invoice,
        quality=assess_quality(invoice, tier, has_text),
        low_confidence_fields=invoice.low_confidence_fields(),
    )
