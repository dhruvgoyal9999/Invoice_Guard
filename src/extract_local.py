"""
FREE tier extractor. Spec Section 8, A-13.

Pure Python. Reads the text layer that born-digital PDFs already carry, using
PyMuPDF. No OCR, no model, no network, no cost.

It cannot read scanned documents -- those have no text layer at all. The
quality gate in extract.py catches that and routes to premium.

Two parsing strategies are used, because invoices need both:

  Scalar fields (invoice number, dates, totals)
      Label matching over the text lines. Vendors word labels differently
      ("Invoice No." / "Bill Number" / "Document Ref" / "Inv #") so we match
      on meaning via patterns, and record what was actually printed.

  Line items
      Geometry, not text. Words are clustered into rows by their y-coordinate
      and split into columns by x-position. Line-item tables differ too much
      between layouts for line-based parsing to hold up.

CONFIDENCE
The text is exact -- these are the characters the PDF actually contains, not a
guess at pixels. So the uncertainty here is in LABEL MATCHING, not character
recognition, and confidence is scored accordingly:
    0.95  matched a known label pattern
    0.85  found by position or structure, no explicit label
    0.70  inferred with a real assumption behind it
"""

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf

from . import config
from .money import rupees_to_paise
from .schemas import ExtractedField, ExtractedInvoice, LineItem, ServicePeriod

CONF_LABELLED = 0.95
CONF_POSITIONAL = 0.85
CONF_INFERRED = 0.70


# ---------------------------------------------------------------------------
# LABEL PATTERNS
# ---------------------------------------------------------------------------
# Ordered most-specific first. Matching is case-insensitive.

LABELS: dict[str, list[str]] = {
    "invoice_number": [
        r"^invoice\s*no\.?\s*:?",
        r"^bill\s*number\s*:?",
        r"^document\s*ref\.?\s*:?",
        r"^inv\s*#",
        r"^invoice\s*number\s*:?",
    ],
    "invoice_date": [
        r"^invoice\s*date\s*:?",
        r"^issue\s*date\s*:?",
        r"^dated\s*:?",
        r"^date\b\s*:?",
    ],
    "po_reference": [
        r"^purchase\s*order\s*:?",
        r"^p\.?\s*o\.?\s*number\s*:?",
        r"^order\s*ref\.?\s*:?",
        r"^ref\s*:?\s*po\b",
        r"^po\s*number\s*:?",
    ],
    "subtotal": [
        r"^taxable\s*value\s*:?",
        r"^sub\s*total\s*:?",
        r"^net\s*amount\s*:?",
        r"^amount\s*\(excl\.?\s*tax\)\s*:?",
        r"^subtotal\s*:?",
    ],
    "total": [
        r"^total\s*payable\s*:?",
        r"^amount\s*due\s*:?",
        r"^grand\s*total\s*:?",
        r"^total\b\s*:?",
    ],
    "service_period": [
        r"^service\s*period\s*:?",
        r"^period\s*covered\s*:?",
    ],
}

# Tax lines carry both the amount and the rate. CGST/SGST split means there
# can be more than one, and both amounts and both rates must be summed.
TAX_LINE = re.compile(
    r"^\s*(igst|cgst|sgst|gst|tax)\b[^0-9]*?(\d+(?:\.\d+)?)\s*%", re.I
)

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z][A-Z0-9])\b")
AMOUNT_RE = re.compile(r"(?:rs\.?\s*)?((?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{1,2})?)", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")


# ---------------------------------------------------------------------------
# PARSING HELPERS
# ---------------------------------------------------------------------------

def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _strip_label(line: str, pattern: str) -> str:
    """Remove a matched label from its line and return whatever followed it."""
    return re.sub(pattern, "", line, count=1, flags=re.I).strip(" :\t")


def _find_labelled(lines: list[str], patterns: list[str]) -> tuple[str, str, int] | None:
    """
    Locate a labelled value.

    Handles both layouts in one pass:
      "Inv #  INV-PIN-4471"   -> value trails the label on the same line
      "Invoice No." / "SL-8834" -> value is on the next line

    Returns (value_text, label_as_printed, line_index) or None.
    """
    for idx, line in enumerate(lines):
        for pattern in patterns:
            m = re.search(pattern, line, flags=re.I)
            if not m:
                continue
            label = m.group(0).strip(" :\t")

            trailing = _strip_label(line, pattern)
            if trailing:
                return trailing, label, idx

            if idx + 1 < len(lines):
                return lines[idx + 1], label, idx
    return None


def _parse_amount_to_paise(text: str) -> int | None:
    """'1,98,500.00' or 'Rs. 2,34,230.00' -> paise. Indian grouping tolerated."""
    if not text:
        return None
    m = AMOUNT_RE.search(text.replace(" ", ""))
    if not m:
        return None
    cleaned = m.group(1).replace(",", "")
    try:
        Decimal(cleaned)
    except InvalidOperation:
        return None
    return rupees_to_paise(cleaned)


def _parse_date_iso(text: str) -> tuple[str, str] | None:
    """Return (ISO date, printed form). Indian DD/MM/YYYY convention -- A-08."""
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= d <= 31 and 1 <= mth <= 12):
        return None
    return f"{y:04d}-{mth:02d}-{d:02d}", m.group(0)


def _missing(reason: str) -> ExtractedField:
    return ExtractedField(value=None, confidence=0.0, found_as=None, reason=reason)


# ---------------------------------------------------------------------------
# LINE ITEMS -- geometric row clustering
# ---------------------------------------------------------------------------

TABLE_START = re.compile(r"^(description|particulars|item|s\.?\s*no)\b", re.I)
TABLE_END = re.compile(
    r"^(taxable\s*value|sub\s*total|net\s*amount|amount\s*\(excl)", re.I
)

# Column headings to skip once the table has started.
HEADER_TOKENS = re.compile(
    r"^(s\.?\s*no|item|description(\s+of\s+services)?|particulars|qty(\s*x\s*rate)?|"
    r"units?|rate|unit\s*price|amount(\s*\(rs\.?\))?|value(\s*\(inr\))?)$",
    re.I,
)

PURE_NUMBER = re.compile(r"^(?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{1,2})?$")
QTY_X_RATE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*[x\u00d7]\s*((?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{1,2})?)$", re.I
)


def _extract_line_items(lines: list[str]) -> tuple[list[LineItem], list[str]]:
    """
    Walk the table as a state machine over text lines.

    PyMuPDF returns the text layer in reading order, which already preserves
    row structure: a description followed by its numbers, then the next
    description. That is more reliable than clustering word coordinates, and
    it survives the fact that one layout merges quantity and rate into a
    single "1 x 16,50,000.00" cell.

    A row is: [optional sequence number] description, then 1-3 numeric cells.
    The last numeric cell is always the amount.
    """
    notes: list[str] = []

    start = next((i for i, ln in enumerate(lines) if TABLE_START.match(ln)), None)
    if start is None:
        return [], ["Could not locate the start of the line-item table"]

    end = next(
        (i for i in range(start + 1, len(lines)) if TABLE_END.match(lines[i])), None
    )
    if end is None:
        return [], ["Could not locate the end of the line-item table"]

    body = [
        ln for ln in lines[start + 1:end]
        if not HEADER_TOKENS.match(ln.strip())
    ]

    items: list[LineItem] = []
    description: str | None = None
    numerics: list[str] = []

    def flush() -> None:
        nonlocal description, numerics
        if description and numerics:
            amount = _parse_amount_to_paise(numerics[-1])
            if amount is not None:
                qty = unit = None
                if len(numerics) >= 3:
                    try:
                        qty = float(numerics[0].replace(",", ""))
                    except ValueError:
                        qty = None
                    unit = _parse_amount_to_paise(numerics[1])
                elif len(numerics) == 2:
                    m = QTY_X_RATE.match(numerics[0])
                    if m:
                        qty = float(m.group(1))
                        unit = _parse_amount_to_paise(m.group(2))
                items.append(
                    LineItem(
                        description=description,
                        qty=qty,
                        unit_price_paise=unit,
                        amount_paise=amount,
                        confidence=CONF_POSITIONAL,
                    )
                )
        description, numerics = None, []

    def is_numeric(ln: str) -> bool:
        return bool(PURE_NUMBER.match(ln) or QTY_X_RATE.match(ln))

    i = 0
    while i < len(body):
        ln = body[i]
        if is_numeric(ln):
            # A bare 1-3 digit integer followed by TEXT is the NEXT row's
            # sequence marker, not this row's amount. Without this lookahead
            # the "2" that starts row two is swallowed as row one's total.
            nxt = body[i + 1] if i + 1 < len(body) else None
            if (
                re.fullmatch(r"\d{1,3}", ln)
                and nxt is not None
                and not is_numeric(nxt)
            ):
                flush()
                i += 1
                continue
            if description is None:
                i += 1
                continue
            numerics.append(ln)
        else:
            flush()
            description = ln
        i += 1
    flush()

    if not items:
        notes.append("Line-item table located but no rows could be parsed")
    return items, notes


# ---------------------------------------------------------------------------
# PUBLIC
# ---------------------------------------------------------------------------

def extract(pdf_path: Path) -> ExtractedInvoice:
    """Read one born-digital invoice. Raises nothing -- absent fields come
    back as nulls with reasons, exactly as the premium tier does."""
    doc = pymupdf.open(pdf_path)
    try:
        text = "\n".join(p.get_text() for p in doc)
        lines = _lines(text)
        line_items, notes = _extract_line_items(lines)
    finally:
        doc.close()

    if not lines:
        return ExtractedInvoice(
            extraction_notes=["No text layer -- this is a scanned document"]
        )

    inv = ExtractedInvoice(line_items=line_items, extraction_notes=notes)

    # --- invoice number ---------------------------------------------------
    hit = _find_labelled(lines, LABELS["invoice_number"])
    if hit:
        raw, label, _ = hit
        value = raw.split()[0] if raw else ""
        inv.invoice_number = ExtractedField(
            value=value, confidence=CONF_LABELLED, found_as=label
        )
    else:
        inv.invoice_number = _missing("No invoice number label found")

    # --- invoice date -----------------------------------------------------
    hit = _find_labelled(lines, LABELS["invoice_date"])
    parsed = _parse_date_iso(hit[0]) if hit else None
    if parsed:
        inv.invoice_date = ExtractedField(
            value=parsed[0], confidence=CONF_LABELLED, found_as=hit[1]
        )
    else:
        anywhere = next((p for p in (_parse_date_iso(ln) for ln in lines) if p), None)
        if anywhere:
            inv.invoice_date = ExtractedField(
                value=anywhere[0], confidence=CONF_INFERRED,
                found_as="unlabelled date on page",
            )
            inv.extraction_notes.append(
                "Invoice date taken from an unlabelled date on the page"
            )
        else:
            inv.invoice_date = _missing("No parseable date found")

    # --- vendor name ------------------------------------------------------
    # Always the first line of the letterhead across all four layouts.
    inv.vendor_name = ExtractedField(
        value=lines[0], confidence=CONF_POSITIONAL, found_as="letterhead (first line)"
    )

    # --- GSTIN ------------------------------------------------------------
    m = GSTIN_RE.search(text.upper())
    if m:
        inv.vendor_gstin = ExtractedField(
            value=m.group(1), confidence=CONF_LABELLED, found_as="GSTIN"
        )
    else:
        inv.vendor_gstin = _missing("No GSTIN pattern found")

    # --- PO reference -----------------------------------------------------
    hit = _find_labelled(lines, LABELS["po_reference"])
    if hit and hit[0]:
        inv.po_reference = ExtractedField(
            value=hit[0].split()[0], confidence=CONF_LABELLED, found_as=hit[1]
        )
    else:
        inv.po_reference = _missing("No PO reference label found")

    # --- subtotal ---------------------------------------------------------
    hit = _find_labelled(lines, LABELS["subtotal"])
    paise = _parse_amount_to_paise(hit[0]) if hit else None
    inv.subtotal_paise = (
        ExtractedField(value=paise, confidence=CONF_LABELLED, found_as=hit[1])
        if paise is not None else _missing("No subtotal label found")
    )

    # --- total ------------------------------------------------------------
    hit = _find_labelled(lines, LABELS["total"])
    paise = _parse_amount_to_paise(hit[0]) if hit else None
    inv.total_paise = (
        ExtractedField(value=paise, confidence=CONF_LABELLED, found_as=hit[1])
        if paise is not None else _missing("No total label found")
    )

    # --- tax and rate -----------------------------------------------------
    # May be one IGST line, or CGST + SGST which must both be summed.
    tax_total = 0
    rate_total = 0.0
    components: list[str] = []

    for idx, line in enumerate(lines):
        m = TAX_LINE.match(line)
        if not m:
            continue
        rate = float(m.group(2))
        amount = _parse_amount_to_paise(_strip_label(line, m.group(0)))
        if amount is None and idx + 1 < len(lines):
            amount = _parse_amount_to_paise(lines[idx + 1])
        if amount is None:
            continue
        tax_total += amount
        rate_total += rate
        components.append(f"{m.group(0).strip()} {amount / 100:,.2f}")

    if components:
        combined = int(rate_total) if rate_total == int(rate_total) else rate_total
        inv.tax_paise = ExtractedField(
            value=tax_total, confidence=CONF_LABELLED,
            found_as=" + ".join(components),
        )
        inv.gst_rate = ExtractedField(
            value=int(rate_total), confidence=CONF_LABELLED,
            found_as=" + ".join(c.split()[0] for c in components),
        )
        if len(components) > 1:
            inv.extraction_notes.append(
                f"Tax split across {len(components)} components, summed to "
                f"{combined}%"
            )
    else:
        inv.tax_paise = _missing("No tax line found")
        inv.gst_rate = _missing("No tax rate found")

    # --- service period ---------------------------------------------------
    hit = _find_labelled(lines, LABELS["service_period"])
    if hit:
        dates = DATE_RE.findall(hit[0])
        if len(dates) >= 2:
            a = f"{int(dates[0][2]):04d}-{int(dates[0][1]):02d}-{int(dates[0][0]):02d}"
            b = f"{int(dates[1][2]):04d}-{int(dates[1][1]):02d}-{int(dates[1][0]):02d}"
            inv.service_period = ExtractedField(
                value=ServicePeriod(from_date=a, to_date=b),
                confidence=CONF_LABELLED, found_as=hit[1],
            )
        else:
            inv.service_period = _missing("Service period label found but no date range")
    else:
        inv.service_period = _missing("No service period on this invoice")

    return inv
