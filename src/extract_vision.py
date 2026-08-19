"""
PREMIUM tier extractor. Spec Section 8.

Sends each invoice page to an OpenAI vision model and gets structured JSON
back. Handles anything the free tier cannot -- scans, rotation, noise, faint
text -- and returns the identical ExtractedInvoice shape.

STRUCTURED OUTPUTS
The request uses Chat Completions with a strict JSON schema, so the model
physically cannot return a malformed shape. That removes a whole class of
parsing failure.

Chat Completions rather than the newer Responses API deliberately: it is what
every OpenAI-compatible gateway supports, including Portkey, whose own docs map
Responses calls back onto Chat. One code path serves both routes rather than
branching on where the request is going.

Strict mode supports only a subset of JSON Schema:
  - every property must appear in "required"
  - "additionalProperties": false everywhere
  - nullable is expressed as a type union, e.g. ["string", "null"]

MONEY
The model returns RUPEE DECIMAL STRINGS ("198500.00"), not paise. Converting
rupees to paise is arithmetic, and asking a language model to do arithmetic on
every field invites silent errors. We convert here with Decimal instead --
the same boundary pattern store.py uses for the CSVs.
"""

import base64
import json
from pathlib import Path

import pymupdf

from . import config
from .money import rupees_to_paise
from .prompts import EXTRACTION_SYSTEM_PROMPT, EXTRACTION_USER_PROMPT
from .schemas import ExtractedField, ExtractedInvoice, LineItem, ServicePeriod


# ---------------------------------------------------------------------------
# JSON SCHEMA
# ---------------------------------------------------------------------------

def _field(value_schema: dict) -> dict:
    """One ExtractedField: a value plus its confidence and provenance."""
    return {
        "type": "object",
        "properties": {
            "value": value_schema,
            "confidence": {"type": "number"},
            "found_as": {"type": ["string", "null"]},
            "reason": {"type": ["string", "null"]},
        },
        "required": ["value", "confidence", "found_as", "reason"],
        "additionalProperties": False,
    }


_STR = {"type": ["string", "null"]}
_INT = {"type": ["integer", "null"]}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": _field(_STR),
        "invoice_date": _field(_STR),
        "vendor_name": _field(_STR),
        "vendor_gstin": _field(_STR),
        "po_reference": _field(_STR),
        "service_period": _field(
            {
                "type": ["object", "null"],
                "properties": {
                    "from_date": {"type": "string"},
                    "to_date": {"type": "string"},
                },
                "required": ["from_date", "to_date"],
                "additionalProperties": False,
            }
        ),
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "qty": {"type": ["number", "null"]},
                    "unit_price_rupees": {"type": ["string", "null"]},
                    "amount_rupees": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "description", "qty", "unit_price_rupees",
                    "amount_rupees", "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "subtotal_rupees": _field(_STR),
        "gst_rate": _field(_INT),
        "tax_rupees": _field(_STR),
        "total_rupees": _field(_STR),
        "extraction_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "invoice_number", "invoice_date", "vendor_name", "vendor_gstin",
        "po_reference", "service_period", "line_items", "subtotal_rupees",
        "gst_rate", "tax_rupees", "total_rupees", "extraction_notes",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

JPEG_QUALITY = 92


def render_pages(pdf_path: Path) -> list[str]:
    """
    Render every page to a base64 data URI.

    Both clean PDFs and scans go through this. One code path, no branching on
    document type -- the model sees a picture either way.

    Each page is encoded as both PNG and JPEG and the smaller one wins, because
    the two formats fail on opposite inputs. PNG compresses flat vector text
    beautifully but cannot compress the gaussian noise in a scan -- one of our
    degraded invoices came to 2.7 MB as PNG versus 0.7 MB as JPEG. Since image
    size drives token cost, that difference is worth one extra encode.
    """
    doc = pymupdf.open(pdf_path)
    try:
        uris = []
        for page in doc:
            pix = page.get_pixmap(dpi=config.RENDER_DPI)
            png = pix.tobytes("png")
            jpg = pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
            if len(png) <= len(jpg):
                data, mime = png, "image/png"
            else:
                data, mime = jpg, "image/jpeg"
            uris.append(
                f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            )
        return uris
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# RESPONSE -> SCHEMA
# ---------------------------------------------------------------------------

def _to_field(raw: dict | None, *, as_paise: bool = False) -> ExtractedField:
    """Convert one raw field object into an ExtractedField."""
    if not isinstance(raw, dict):
        return ExtractedField(
            value=None, confidence=0.0, reason="Field absent from model response"
        )

    value = raw.get("value")
    confidence = float(raw.get("confidence") or 0.0)
    found_as = raw.get("found_as")
    reason = raw.get("reason")

    if value is None:
        return ExtractedField(
            value=None,
            confidence=0.0,
            found_as=found_as,
            reason=reason or "Not present on the document",
        )

    if as_paise:
        try:
            value = rupees_to_paise(value)
        except Exception:
            return ExtractedField(
                value=None,
                confidence=0.0,
                found_as=found_as,
                reason=f"Unparseable amount from model: {raw.get('value')!r}",
            )

    return ExtractedField(
        value=value, confidence=confidence, found_as=found_as, reason=None
    )


def response_to_invoice(payload: dict) -> ExtractedInvoice:
    """
    Map the model's JSON onto ExtractedInvoice, converting rupees to paise.

    Separated from the API call so it can be tested without a network round
    trip -- and so a cached raw response can be replayed.
    """
    inv = ExtractedInvoice()

    inv.invoice_number = _to_field(payload.get("invoice_number"))
    inv.invoice_date = _to_field(payload.get("invoice_date"))
    inv.vendor_name = _to_field(payload.get("vendor_name"))
    inv.vendor_gstin = _to_field(payload.get("vendor_gstin"))
    inv.po_reference = _to_field(payload.get("po_reference"))

    inv.subtotal_paise = _to_field(payload.get("subtotal_rupees"), as_paise=True)
    inv.tax_paise = _to_field(payload.get("tax_rupees"), as_paise=True)
    inv.total_paise = _to_field(payload.get("total_rupees"), as_paise=True)
    inv.gst_rate = _to_field(payload.get("gst_rate"))

    # --- service period -----------------------------------------------------
    raw_period = payload.get("service_period") or {}
    period_value = raw_period.get("value") if isinstance(raw_period, dict) else None
    if isinstance(period_value, dict):
        try:
            inv.service_period = ExtractedField(
                value=ServicePeriod(
                    from_date=period_value["from_date"],
                    to_date=period_value["to_date"],
                ),
                confidence=float(raw_period.get("confidence") or 0.0),
                found_as=raw_period.get("found_as"),
            )
        except (KeyError, ValueError) as exc:
            inv.service_period = ExtractedField(
                value=None, confidence=0.0,
                reason=f"Service period returned but unusable: {exc}",
            )
    else:
        inv.service_period = ExtractedField(
            value=None,
            confidence=0.0,
            reason=(raw_period.get("reason") if isinstance(raw_period, dict) else None)
            or "No service period on this invoice",
        )

    # --- line items ---------------------------------------------------------
    items: list[LineItem] = []
    for raw in payload.get("line_items") or []:
        try:
            amount = rupees_to_paise(raw["amount_rupees"])
        except Exception:
            inv.extraction_notes.append(
                f"Dropped a line item with an unparseable amount: "
                f"{raw.get('description', '?')!r}"
            )
            continue
        unit = None
        if raw.get("unit_price_rupees"):
            try:
                unit = rupees_to_paise(raw["unit_price_rupees"])
            except Exception:
                unit = None
        items.append(
            LineItem(
                description=str(raw.get("description") or "").strip() or "(no description)",
                qty=raw.get("qty"),
                unit_price_paise=unit,
                amount_paise=amount,
                confidence=float(raw.get("confidence") or 0.0),
            )
        )
    inv.line_items = items

    # Model notes go last so any parsing complaints we raised stay at the top.
    for note in payload.get("extraction_notes") or []:
        inv.extraction_notes.append(str(note))

    return inv


# ---------------------------------------------------------------------------
# API CALL
# ---------------------------------------------------------------------------

def extract(pdf_path: Path, creds=None) -> ExtractedInvoice:
    """
    Read one invoice with a vision model.

    Credentials come from src.credentials -- runtime first (what the UI
    collected), environment second. The route may be OpenAI direct or a Portkey
    gateway; both speak the same API, so nothing below this line changes.
    """
    from . import credentials as cred

    creds = creds or cred.current()
    problem = creds.missing()
    if problem:
        raise RuntimeError(problem)

    images = render_pages(pdf_path)
    if not images:
        return ExtractedInvoice(
            extraction_notes=["Document has no renderable pages"]
        )

    content: list[dict] = [{"type": "text", "text": EXTRACTION_USER_PROMPT}]
    for uri in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": uri, "detail": "high"},
        })

    response = creds.build_client().chat.completions.create(
        model=creds.effective_model(),
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_extraction",
                "schema": INVOICE_SCHEMA,
                "strict": True,
            },
        },
        max_tokens=config.OPENAI_MAX_OUTPUT_TOKENS,
    )

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return ExtractedInvoice(
            extraction_notes=["Model returned an empty response"]
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ExtractedInvoice(
            extraction_notes=[f"Model returned unparseable JSON: {exc}"]
        )

    return response_to_invoice(payload)
