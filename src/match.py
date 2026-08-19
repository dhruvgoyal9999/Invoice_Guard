"""
Invoice -> PO matching. Spec Section 9.

Pure Python. No model involvement, no network, fully deterministic.

    Layer 1  explicit PO reference printed on the invoice   -> HIGH
    Layer 2  inferred from vendor + amount + date window    -> MEDIUM
    Layer 3  several plausible POs, returned RANKED         -> AMBIGUOUS
    Layer 4  nothing found                                  -> NO_MATCH + reason

Layer 3 never silently picks the best candidate. When more than one PO fits,
the system reports all of them with scores and lets a human choose. A confident
wrong match is worse than an admitted ambiguity -- it pays the wrong contract
and the trace looks clean.
"""

import re
from datetime import date, datetime

from rapidfuzz import fuzz

from . import config, store
from .schemas import (
    ExtractedInvoice,
    MatchConfidence,
    MatchingResult,
    POCandidate,
    PurchaseOrder,
    Vendor,
)


# ---------------------------------------------------------------------------
# VENDOR NAME NORMALISATION
# ---------------------------------------------------------------------------

_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalise_vendor_name(name: str) -> str:
    """
    Reduce a vendor name to its distinctive core. Spec 9.2.

    Real invoices carry all of these for one vendor:
        Sharma Logistics
        SHARMA LOGISTICS PVT. LTD.
        Sharma Logistics Private Limited

    Exact string matching fails on every pair. Uppercasing, dropping
    punctuation and stripping legal-form suffixes leaves "SHARMA LOGISTICS"
    in all three cases.
    """
    if not name:
        return ""
    text = _PUNCT.sub(" ", name.upper())
    tokens = [t for t in _SPACES.split(text) if t]
    tokens = [t for t in tokens if t not in config.VENDOR_NAME_NOISE_TOKENS]
    return " ".join(tokens).strip()


def _best_name_score(candidate: str, vendor: Vendor) -> int:
    """Best fuzzy score across a vendor's legal name and every alias."""
    target = normalise_vendor_name(candidate)
    if not target:
        return 0
    names = [vendor.legal_name, *vendor.aliases]
    return max(
        int(fuzz.token_set_ratio(target, normalise_vendor_name(n))) for n in names
    )


def match_vendor(
    invoice: ExtractedInvoice,
) -> tuple[Vendor | None, int, str]:
    """
    Identify the vendor. Returns (vendor, score, how).

    GSTIN is tried first and wins outright when present. It is a registered
    government identifier -- an exact match is far stronger evidence than any
    amount of string similarity on a company name.
    """
    vendors = store.get_all_vendors()

    gstin = invoice.vendor_gstin.value
    if gstin:
        key = gstin.strip().upper()
        for v in vendors:
            if v.gstin and v.gstin.strip().upper() == key:
                # GSTIN wins, but a name that disagrees with it is worth
                # surfacing: an invoice carrying one company's name and
                # another's tax number is a classic redirection attempt.
                name = invoice.vendor_name.value
                if name:
                    name_score = _best_name_score(name, v)
                    if name_score < config.VENDOR_FUZZY_THRESHOLD:
                        return v, 100, (
                            f"GSTIN exact match, but the printed vendor name "
                            f"'{name}' scores only {name_score}/100 against "
                            f"{v.legal_name} - name and tax number disagree"
                        )
                return v, 100, "GSTIN exact match"

    name = invoice.vendor_name.value
    if not name:
        return None, 0, "No vendor name or GSTIN on the invoice"

    scored = sorted(
        ((v, _best_name_score(name, v)) for v in vendors),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best, score = scored[0]
    if score >= config.VENDOR_FUZZY_THRESHOLD:
        return best, score, f"Fuzzy name match ({score}/100)"

    return None, score, (
        f"No vendor scored above {config.VENDOR_FUZZY_THRESHOLD}; "
        f"best was {best.legal_name} at {score}"
    )


# ---------------------------------------------------------------------------
# PO REFERENCE NORMALISATION
# ---------------------------------------------------------------------------

# Characters an OCR or a poor scan routinely confuses inside a numeric field.
_OCR_DIGIT_REPAIRS = str.maketrans({"O": "0", "Q": "0", "D": "0",
                                    "I": "1", "L": "1",
                                    "S": "5", "B": "8", "Z": "2"})


def normalise_po_reference(raw: str) -> str:
    """
    'PO-1010' / 'po1010' / 'P.O. 1010' / 'PO # 1010' all become 'PO-1010'.

    Strips everything that is not a letter or digit, then reinserts the hyphen
    between the alphabetic prefix and the numeric part.
    """
    if not raw:
        return ""
    compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
    m = re.match(r"^([A-Z]+)0*(\d+)$", compact)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return compact


def repair_candidates(raw: str) -> list[str]:
    """
    Plausible repairs of a PO reference damaged by a scan.

    'PO-1OO3' should become 'PO-1003', but the split between prefix and number
    is genuinely ambiguous: the letter O is both a valid prefix character (as
    in "PO") and the classic stand-in for zero. Rather than guess one split,
    generate a candidate for every prefix length and let the caller try each
    against real PO numbers. The store decides which one exists.

    Fuzzy string distance alone is not enough here: 'PO1OO3' against 'PO1003'
    scores in the low eighties, too close to the noise floor to threshold on.
    """
    if not raw:
        return []
    compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not compact:
        return []

    seen: list[str] = []
    for split in range(1, min(len(compact), 5)):
        prefix, tail = compact[:split], compact[split:]
        if not prefix.isalpha():
            continue
        repaired = prefix + tail.translate(_OCR_DIGIT_REPAIRS)
        normalised = normalise_po_reference(repaired)
        if normalised and normalised not in seen:
            seen.append(normalised)
    return seen


def _parse_invoice_date(invoice: ExtractedInvoice) -> date | None:
    raw = invoice.invoice_date.value
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# CANDIDATE SCORING
# ---------------------------------------------------------------------------

def _score_candidate(
    po: PurchaseOrder,
    subtotal_paise: int | None,
    invoice_date: date | None,
) -> tuple[int, str]:
    """
    Score how well an invoice fits a PO, 0-100, on amount and date proximity.

    Amount carries more weight than date: a vendor billing close to the
    remaining balance is strong evidence, whereas invoice dates drift for all
    sorts of innocent reasons.
    """
    parts: list[str] = []
    amount_score = 0
    date_score = 0

    remaining = po.remaining_balance_paise
    if subtotal_paise is not None and remaining > 0:
        ratio = abs(subtotal_paise - remaining) / remaining
        amount_score = int(max(0.0, 1.0 - ratio / config.PO_AMOUNT_MATCH_WINDOW) * 70)
        parts.append(f"amount within {ratio * 100:.1f}% of remaining balance")

    if invoice_date is not None:
        days = abs((invoice_date - po.po_date).days)
        date_score = int(max(0.0, 1.0 - days / config.PO_DATE_WINDOW_DAYS) * 30)
        parts.append(f"{days} days from PO date")

    return amount_score + date_score, "; ".join(parts) or "no scoring signal"


# ---------------------------------------------------------------------------
# MATCHING
# ---------------------------------------------------------------------------

def match_invoice(invoice: ExtractedInvoice) -> MatchingResult:
    """Run the four layers in order, stopping at the first success."""
    notes: list[str] = []
    vendor, vendor_score, vendor_how = match_vendor(invoice)
    if vendor:
        notes.append(f"Vendor identified as {vendor.vendor_id} - {vendor_how}")
    else:
        notes.append(f"Vendor not identified - {vendor_how}")

    subtotal = invoice.subtotal_paise.value
    inv_date = _parse_invoice_date(invoice)

    base = {
        "vendor_id": vendor.vendor_id if vendor else None,
        "vendor_name_matched": vendor.legal_name if vendor else None,
        "vendor_match_score": vendor_score,
    }

    # ---- Layer 1: explicit PO reference ---------------------------------
    raw_ref = invoice.po_reference.value
    if raw_ref:
        ref = normalise_po_reference(raw_ref)
        po = store.get_po(ref)
        if po:
            return MatchingResult(
                match_status="MATCHED", match_layer=1,
                match_confidence=MatchConfidence.HIGH, po_number=po.po_number,
                candidates_considered=[
                    POCandidate(po_number=po.po_number, score=100,
                                reason=f"PO reference '{raw_ref}' printed on the invoice")
                ],
                notes=notes, **base,
            )

        # The reference is printed but does not resolve. Before giving up, try
        # repairing the letter-for-digit confusions a scan introduces --
        # 'PO-1OO3' with letter O instead of zero is exactly what a scan does.
        po = None
        for candidate in repair_candidates(raw_ref):
            if candidate == ref:
                continue
            po = store.get_po(candidate)
            if po:
                break
        if po:
            notes.append(
                f"Printed reference '{raw_ref}' did not resolve; recovered "
                f"{po.po_number} after repairing likely scan character errors"
            )
            return MatchingResult(
                match_status="MATCHED", match_layer=1,
                match_confidence=MatchConfidence.MEDIUM, po_number=po.po_number,
                candidates_considered=[
                    POCandidate(po_number=po.po_number, score=95,
                                reason=f"Character-repaired match to '{raw_ref}'")
                ],
                notes=notes, **base,
            )

        known = [p.po_number for p in store.get_all_pos()]
        near = sorted(
            ((n, int(fuzz.ratio(repaired, n))) for n in known),
            key=lambda pair: pair[1], reverse=True,
        )
        if near and near[0][1] >= 90:
            po = store.get_po(near[0][0])
            notes.append(
                f"Printed reference '{raw_ref}' did not resolve; recovered "
                f"{po.po_number} by near match ({near[0][1]}/100)"
            )
            return MatchingResult(
                match_status="MATCHED", match_layer=1,
                match_confidence=MatchConfidence.MEDIUM, po_number=po.po_number,
                candidates_considered=[
                    POCandidate(po_number=po.po_number, score=near[0][1],
                                reason=f"Near match to printed reference '{raw_ref}'")
                ],
                notes=notes, **base,
            )

        notes.append(
            f"PO reference '{raw_ref}' is printed on the invoice but no such "
            f"PO exists; falling back to inference"
        )

    # ---- Layer 2/3: infer from vendor + amount + date --------------------
    if vendor is None:
        return MatchingResult(
            match_status="NO_MATCH", match_layer=4,
            match_confidence=MatchConfidence.NONE,
            notes=notes + ["Cannot infer a PO without knowing the vendor"],
            **base,
        )

    open_pos = store.find_pos_by_vendor(vendor.vendor_id, open_only=True)
    if not open_pos:
        return MatchingResult(
            match_status="NO_MATCH", match_layer=4,
            match_confidence=MatchConfidence.NONE,
            notes=notes + [f"No open POs on record for {vendor.vendor_id}"],
            **base,
        )

    candidates: list[POCandidate] = []
    rejected: list[str] = []
    for po in open_pos:
        if subtotal is not None and po.remaining_balance_paise > 0:
            ratio = abs(subtotal - po.remaining_balance_paise) / po.remaining_balance_paise
            if ratio > config.PO_AMOUNT_MATCH_WINDOW:
                rejected.append(f"{po.po_number} (amount off by {ratio * 100:.0f}%)")
                continue
        if inv_date is not None:
            days = abs((inv_date - po.po_date).days)
            if days > config.PO_DATE_WINDOW_DAYS:
                rejected.append(f"{po.po_number} ({days} days from PO date)")
                continue
        score, why = _score_candidate(po, subtotal, inv_date)
        candidates.append(POCandidate(po_number=po.po_number, score=score, reason=why))

    candidates.sort(key=lambda c: c.score, reverse=True)

    if rejected:
        notes.append("Ruled out: " + ", ".join(rejected))

    if not candidates:
        return MatchingResult(
            match_status="NO_MATCH", match_layer=4,
            match_confidence=MatchConfidence.NONE,
            candidates_considered=[],
            notes=notes + [
                f"{len(open_pos)} open PO(s) for {vendor.vendor_id}, but none "
                f"fell within the amount and date windows"
            ],
            **base,
        )

    if len(candidates) == 1:
        return MatchingResult(
            match_status="MATCHED", match_layer=2,
            match_confidence=MatchConfidence.MEDIUM,
            po_number=candidates[0].po_number,
            candidates_considered=candidates,
            notes=notes + ["Inferred from vendor, amount and date - no PO reference printed"],
            **base,
        )

    # Layer 3: several fit. Report them all; do not choose.
    return MatchingResult(
        match_status="AMBIGUOUS", match_layer=3,
        match_confidence=MatchConfidence.AMBIGUOUS,
        po_number=None,
        candidates_considered=candidates,
        notes=notes + [
            f"{len(candidates)} POs fit within tolerance; a human must choose"
        ],
        **base,
    )
