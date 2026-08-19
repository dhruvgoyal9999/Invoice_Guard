# PS-1 · Invoice Processing — Build Specification

**Project:** Invoice → Decision engine with full audit trace
**Version:** 1.0 (Phase 0 — locked before coding begins)
**Date:** 9 August 2026

---

## How to use this document

This is the single source of truth for the build. Every number, rule and threshold lives here.

Two rules for yourself while building:

1. **If you change a number, change it here first, then in code.** Otherwise you will end up with three different tolerance values in three different files. This is the most common way a vibe-coded project falls apart.
2. **Paste the relevant section into your AI coding tool as context before each phase.** Do not ask it to "build an invoice processor" — ask it to "implement Section 10, rule R-302, exactly as written."

Anything marked **[ASSUMPTION]** is a decision made to keep the project moving. Each one is numbered so you can defend it in a review, or change it later without hunting through the document.

---

## 1. What we are building — in one paragraph

A system that takes a vendor invoice (PDF or scanned image), pulls the numbers out of it, finds the matching purchase order, runs a fixed set of business rules against it, and outputs one of four decisions — along with a complete record of how that decision was reached. A human can open any decision and see every field that was extracted, how confident the system was, every rule that ran, and exactly which rule caused the outcome.

**The thing being graded is not the extraction. It is the reasoning trail.** Many people will build something that reads a PDF. Fewer will build something that can explain itself.

---

## 2. Glossary — so we never argue about words

| Term | Meaning in this project |
|---|---|
| **PO** | Purchase order. The commitment the company made *before* the goods arrived. Our source of truth. |
| **PO total** | The pre-tax (taxable) value of the PO. **Tax is never included.** See Assumption A-03. |
| **Subtotal** | The pre-tax value on the *invoice*. This is what we compare to the PO. |
| **Total / Grand total** | Subtotal + tax. We extract it, but we do **not** compare it to the PO. |
| **Already invoiced** | Sum of the subtotals of all previously accepted invoices against a PO. |
| **Remaining balance** | PO total − already invoiced. What is still legitimately billable. |
| **Overage** | How much an invoice exceeds the remaining balance. Zero or negative means no overage. |
| **Tolerance** | The maximum overage we are willing to accept automatically. |
| **2-way match** | Comparing invoice against PO only. This is what we are building. |
| **3-way match** | Invoice vs PO vs goods-receipt note (GRN). Out of scope — see Section 18. |
| **Trace** | The complete JSON record of everything that happened for one invoice. |
| **Blocker / Critical / Warning / Info** | Severity levels attached to rules. They drive the final decision. |

---

## 3. The one architectural principle

> **The AI model extracts. Python decides.**

The language model is used for exactly one job: looking at an invoice image and returning structured JSON. It never decides whether to approve anything.

Every rule, every comparison, every threshold is plain deterministic Python.

**Why this matters, in plain terms:**

- The same invoice always produces the same decision. Run it a hundred times, get the same answer a hundred times.
- You can point at the exact line of code that caused a rejection.
- If a rule is wrong, you fix one function — you do not re-engineer a prompt and hope.
- In a real finance system, an approval that cannot be reproduced is not an approval. It is a guess.

There is exactly one place a model is allowed near the decision: writing the plain-English *summary sentence* at the end, describing a decision that has **already been made**. It describes; it does not decide.

---

## 4. Assumptions register

These are locked for v1. Each is a real decision with a real reason.

| # | Assumption | Why |
|---|---|---|
| **A-01** | All amounts are in **Indian Rupees (INR)**. Single currency per PO. | Keeps FX out of scope. A currency-mismatch rule still exists (R-301) to catch a wrong-currency invoice. |
| **A-02** | Tax regime is **Indian GST**. Valid slabs: 0%, 5%, 12%, 18%, 28%. | Lets us write a genuine tax-validity rule instead of ignoring tax. |
| **A-03** | **PO totals are pre-tax.** Invoices arrive with GST added on top. | This is how POs are actually issued. See Section 4.1 — this is the single most important assumption in the document. |
| **A-04** | One invoice maps to **at most one PO**. | Multi-PO invoices are real but would double the matching complexity. Noted as a known limitation. |
| **A-05** | **2-way match only** (invoice vs PO). No goods-receipt data. | Deliberate scope choice. Section 18 explains how to extend to 3-way. |
| **A-06** | PO master and vendor master are **local CSV files** loaded into SQLite. | No real ERP available. The interface is designed so a real ERP could be swapped in behind it. |
| **A-07** | All money is stored and compared as **integer paise**. | Floating point will eventually let 1.5000001% slip through. Integers cannot. |
| **A-08** | Dates are Indian convention **DD/MM/YYYY**, but the parser accepts several formats. | Vendor PDFs are inconsistent. Ambiguous dates (e.g. 05/06/2026) are flagged, not guessed. |
| **A-09** | An invoice's **service period** may be extracted when present. | Needed for the recurring-billing edge case (EC-2). Absent on most invoices; that is fine. |
| **A-10** | Vendor bank details are **not** validated against a bank master in v1. | Belongs to PS-2 (vendor onboarding). Out of scope here. |
| **A-11** | The system is **single-user, batch or one-at-a-time**. No concurrency handling. | A real system needs row locking on `already_invoiced`. Noted as a limitation, not built. |
| **A-12** | Rounding tolerance for arithmetic checks is **₹1.00**. | Vendor systems round differently. Anything within a rupee is not a real discrepancy. |

### 4.1 Why Assumption A-03 will make or break the build

This deserves its own paragraph because getting it wrong silently breaks everything.

A PO is raised for ₹1,00,000. The vendor invoices ₹1,00,000 plus 18% GST = ₹1,18,000 total.

If you compare the invoice **total** (₹1,18,000) against the PO (₹1,00,000), the system sees an 18% overage against a 1.5% tolerance and rejects it. **Every single normal invoice fails.** You then start loosening the tolerance to make things pass, and the rule becomes meaningless.

The fix is simple and must be built in from day one:

- Compare **invoice subtotal** ↔ **PO total**. Both pre-tax. Like for like.
- Validate tax **separately**, as its own rules (R-401 to R-403).

This also gives you three extra meaningful rules for free, and it is the kind of detail that shows you understand how AP actually works.

---

## 5. Configuration — every tunable number in one place

These all live in `src/config.py`. Nothing in this list may be hard-coded anywhere else.

```python
# ---------- MONEY ----------
CURRENCY = "INR"
# All money handled as integer paise. 1 rupee = 100 paise.

# ---------- TOLERANCE (over-billing) ----------
TOLERANCE_PERCENT       = 1.5        # percent of PO total
TOLERANCE_ABSOLUTE_CAP  = 10_000_00  # ₹10,000 expressed in paise
# Allowed overage = min(percentage amount, absolute cap)
# Under-billing has NO limit. See Section 10, R-305.

# ---------- FLAGGING ----------
TOLERANCE_CONSUMPTION_FLAG = 0.50    # used >50% of allowance -> flag, don't block

# ---------- EXTRACTION CONFIDENCE ----------
CONFIDENCE_CRITICAL   = 0.80   # invoice no., date, vendor, subtotal, total
CONFIDENCE_SUPPORTING = 0.70   # line items, tax, PO ref, GSTIN

# ---------- MATCHING ----------
VENDOR_FUZZY_THRESHOLD   = 85   # rapidfuzz token_set_ratio, 0-100
PO_AMOUNT_MATCH_WINDOW   = 0.10 # ±10% when searching for a PO by amount
PO_DATE_WINDOW_DAYS      = 120  # invoice must fall within this of PO date

# ---------- DUPLICATES ----------
NEAR_DUPLICATE_WINDOW_DAYS = 30

# ---------- ARITHMETIC ----------
ROUNDING_TOLERANCE = 1_00       # ₹1.00 in paise

# ---------- TAX ----------
VALID_GST_SLABS = [0, 5, 12, 18, 28]
GSTIN_LENGTH = 15

# ---------- DATES ----------
ACCEPTED_DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y",
                         "%d %B %Y", "%Y-%m-%d", "%d.%m.%Y"]
```

---

## 6. The tolerance rule, explained properly

This is the centrepiece of the whole system, so it gets a full worked treatment.

### 6.1 The formula

```
remaining_balance = po_total − already_invoiced

allowed_overage   = min( TOLERANCE_PERCENT% × po_total,
                         TOLERANCE_ABSOLUTE_CAP )

overage           = invoice_subtotal − remaining_balance

breach            = overage > allowed_overage        # strictly greater than
```

Three details that matter:

- **The percentage is always taken on the full PO total, not the remaining balance.** The allowance belongs to the contract, not to each individual invoice. Otherwise a vendor could split into ten invoices and get ten separate allowances.
- **Use `>`, not `>=`.** An invoice landing exactly on the limit passes. This gives you a clean boundary test.
- **If `overage` is zero or negative, there is no breach.** Under-billing never fails. See Section 10, R-305.

### 6.2 The crossover point

Because we take the *smaller* of two numbers, which one binds depends on the size of the PO.

The two are equal when `1.5% × PO = ₹10,000`, i.e. **PO = ₹6,66,667**.

- Below ₹6,66,667 → the **percentage** binds (allowance is under ₹10,000)
- Above ₹6,66,667 → the **cap** binds (allowance is frozen at ₹10,000)

| PO total | 1.5% of PO | ₹10,000 cap | Allowed overage | What binds |
|---|---|---|---|---|
| ₹50,000 | ₹750 | ₹10,000 | **₹750** | percentage |
| ₹2,00,000 | ₹3,000 | ₹10,000 | **₹3,000** | percentage |
| ₹6,66,667 | ₹10,000 | ₹10,000 | **₹10,000** | crossover |
| ₹10,00,000 | ₹15,000 | ₹10,000 | **₹10,000** | cap |
| ₹50,00,000 | ₹75,000 | ₹10,000 | **₹10,000** | cap |

**Build test invoices on both sides of ₹6,66,667.** An invoice that would pass on percentage but fails on the cap is the single cleanest proof that you implemented a dual-threshold rule and not just one number.

### 6.3 Worked examples

**Example 1 — comfortable pass (percentage binds)**
```
PO-1001   po_total = ₹2,00,000   already_invoiced = ₹0
remaining = ₹2,00,000
allowed   = min(₹3,000, ₹10,000) = ₹3,000
invoice subtotal = ₹2,00,900
overage   = ₹900  →  900 ≤ 3,000  →  PASS
consumption = 900 / 3,000 = 30%  →  below 50%, no flag
DECISION: AUTO_APPROVE
```

**Example 2 — passes but eats most of the allowance**
```
PO-1001   allowed = ₹3,000
invoice subtotal = ₹2,02,600
overage   = ₹2,600  →  ≤ 3,000  →  PASS (R-302)
consumption = 2,600 / 3,000 = 87%  →  above 50%  →  R-304 WARNING
DECISION: APPROVE_WITH_FLAG
```

**Example 3 — the cap binds (this is EC-3)**
```
PO-1005   po_total = ₹20,00,000   already_invoiced = ₹0
allowed   = min(₹30,000, ₹10,000) = ₹10,000
invoice subtotal = ₹20,25,000
overage   = ₹25,000  →  25,000 > 10,000  →  FAIL

Note: 25,000 / 20,00,000 = 1.25%, which is UNDER the 1.5% threshold.
A percentage-only implementation would wrongly approve this.
DECISION: HOLD_FOR_REVIEW
```

**Example 4 — split invoicing, landing exactly on the boundary (this is EC-1)**
```
PO-1010   po_total = ₹10,00,000
allowed overage = min(₹15,000, ₹10,000) = ₹10,000

Invoice A: subtotal ₹4,00,000
  remaining = ₹10,00,000 → overage = −₹6,00,000 → under → PASS
  already_invoiced becomes ₹4,00,000

Invoice B: subtotal ₹3,50,000
  remaining = ₹6,00,000  → overage = −₹2,50,000 → under → PASS
  already_invoiced becomes ₹7,50,000

Invoice C: subtotal ₹2,60,000
  remaining = ₹2,50,000  → overage = ₹10,000
  10,000 > 10,000 is FALSE → PASS, exactly on the line
  already_invoiced becomes ₹10,10,000
  consumption = 100% → R-304 WARNING
DECISION: APPROVE_WITH_FLAG
```

Notice what happened in Example 4: **split invoicing needed no special code.** Because tolerance is checked against *remaining balance* rather than PO total, progressive billing falls out of the ordinary path. That is the mark of a well-chosen rule, and it is worth calling out explicitly in your README.

### 6.4 Integer arithmetic — do this, not that

```python
# WRONG — floating point will eventually betray you
allowed = min(po_total * 0.015, 10000)

# RIGHT — integer paise. // already floors, so no float ever appears.
pct_allowance = (po_total_paise * 15) // 1000        # 1.5% = 15/1000
allowed       = min(pct_allowance, TOLERANCE_ABSOLUTE_CAP)

# Check: PO ₹2,00,000 -> 20000000 paise
# (20000000 * 15) // 1000 = 300000 paise = ₹3,000  ✓
```

Store paise everywhere internally. Convert to rupees only when displaying to a human.

---

## 7. Data model

### 7.1 `po_master.csv`

| Column | Type | Notes |
|---|---|---|
| `po_number` | string | Primary key. Format `PO-####`. |
| `vendor_id` | string | Foreign key to vendor master. |
| `vendor_name` | string | Denormalised for convenience. |
| `po_date` | date | When the PO was raised. |
| `po_total_paise` | integer | **Pre-tax.** See A-03. |
| `currency` | string | `INR` for all rows in v1. |
| `already_invoiced_paise` | integer | Starts at 0. Updated as invoices are accepted. |
| `status` | enum | `OPEN` / `CLOSED` / `CANCELLED` |
| `expected_gst_rate` | integer | Expected slab for this PO's goods/services. |
| `line_items` | JSON string | `[{description, qty, unit_price_paise}]` |
| `valid_until` | date | Used by R-603. |

The `already_invoiced_paise` column is what makes split invoicing work. **Build it in now.** Retrofitting it later means touching the matcher, the rules and the trace all at once.

### 7.2 `vendor_master.csv`

| Column | Type | Notes |
|---|---|---|
| `vendor_id` | string | Primary key. |
| `legal_name` | string | Name as registered. |
| `aliases` | JSON list | Trading names, abbreviations. Helps fuzzy matching. |
| `gstin` | string | 15 characters. |
| `is_approved` | boolean | **False here means automatic rejection.** |
| `onboarded_date` | date | Used to detect first-ever invoice (R-203). |

### 7.3 Extracted invoice schema

Every field is an object, never a bare value. This is what makes confidence tracking possible.

```json
{
  "invoice_number":  {"value": "INV-2024-887", "confidence": 0.97, "found_as": "Invoice No."},
  "invoice_date":    {"value": "2026-03-14",   "confidence": 0.93, "found_as": "Dated"},
  "vendor_name":     {"value": "Sharma Logistics Pvt Ltd", "confidence": 0.95, "found_as": "header"},
  "vendor_gstin":    {"value": "27AABCS1429B1ZX", "confidence": 0.88, "found_as": "GSTIN"},
  "po_reference":    {"value": "PO-1010",     "confidence": 0.91, "found_as": "Ref: PO"},
  "service_period":  {"value": {"from": "2026-03-01", "to": "2026-03-15"}, "confidence": 0.84},
  "line_items": [
    {"description": "Freight — Mumbai to Pune", "qty": 12,
     "unit_price_paise": 350000, "amount_paise": 4200000, "confidence": 0.90}
  ],
  "subtotal_paise":  {"value": 4200000, "confidence": 0.96, "found_as": "Taxable Value"},
  "gst_rate":        {"value": 18,      "confidence": 0.94, "found_as": "IGST 18%"},
  "tax_paise":       {"value": 756000,  "confidence": 0.94, "found_as": "IGST"},
  "total_paise":     {"value": 4956000, "confidence": 0.97, "found_as": "Grand Total"},
  "extraction_notes": ["Scanned document, slight rotation corrected"]
}
```

**Nulls are a valid, expected answer.** If the invoice number genuinely is not on the page, the model returns:

```json
"invoice_number": {"value": null, "confidence": 0.0,
                   "found_as": null, "reason": "No invoice number found on document"}
```

It must never invent a plausible-looking one. Say this explicitly in the extraction prompt.

---

## 8. Extraction layer

### 8.1 Approach

Send **every** invoice to the vision model as a rendered page image — clean PDFs included. One code path handles both born-digital and scanned documents, and you skip OCR tooling entirely.

Pipeline: `PDF → PyMuPDF render at 200 DPI → PNG → vision model → JSON → validate against Pydantic schema`

If a PDF has multiple pages, render each and send them together in one request.

### 8.2 Prompt design rules

The extraction prompt must state, in this order:

1. Return **only** JSON matching the provided schema. No prose, no markdown fences.
2. Every field carries `value`, `confidence` (0.0–1.0) and `found_as` (the literal label seen on the document).
3. **If a field is not present, return null and explain why. Never guess or infer a value.**
4. Amounts must be returned in **paise as integers**. ₹4,200.50 becomes `420050`.
5. Do not perform arithmetic. Report what is printed. If the document's own maths is wrong, that is a finding, not something to silently fix.

Point 5 is subtle and important. If the model helpfully "corrects" a subtotal, you lose the ability to detect a vendor whose invoice does not add up.

### 8.3 Post-extraction self-checks (in Python, not the model)

- Do the line item amounts sum to the subtotal, within ₹1?
- Does `subtotal + tax = total`, within ₹1?
- Does `subtotal × gst_rate` equal the tax, within ₹1?
- Is the date parseable, and is it real (not 31 February)?
- Is the GSTIN 15 characters?

Each check feeds a rule in Section 10. None of them silently repair anything.

### 8.4 Cost and speed note

Roughly 1–3 seconds and a fraction of a rupee per page. For a 20-invoice corpus, **cache every extraction result to disk keyed by file hash.** You will re-run the pipeline dozens of times while tuning rules, and you do not want to pay for or wait on re-extraction each time. This one decision saves hours.

---

## 9. Matching engine

Pure Python. No model involvement.

### 9.1 Layered strategy — stop at the first success

**Layer 1 — Explicit PO reference.**
A PO reference was extracted. Normalise it (uppercase, strip spaces, handle `PO1010` / `PO-1010` / `P.O. 1010`) and look it up directly. If found, done — confidence `HIGH`.

**Layer 2 — Vendor + amount + date window.**
No usable PO reference. Search for open POs where:
- vendor matches (see 9.2), **and**
- invoice subtotal falls within ±10% of the PO's remaining balance, **and**
- invoice date is within 120 days of the PO date.

One result → confidence `MEDIUM`. Proceed.

**Layer 3 — Multiple candidates.**
Score each candidate and return them **ranked**. Do not silently pick the best one. The system reports "3 possible POs, here they are, a human should choose." Confidence `AMBIGUOUS`.

**Layer 4 — No candidate.**
Return `NO_MATCH` with a written reason: no vendor found / vendor found but no open POs / open POs exist but none within the amount window.

### 9.2 Vendor name normalisation

Real invoices say "Sharma Logistics", "SHARMA LOGISTICS PVT. LTD.", "Sharma Logistics Private Limited". Exact string matching fails immediately.

Normalise before comparing:
1. Uppercase
2. Strip punctuation
3. Collapse multiple spaces
4. Remove suffixes: `PVT`, `PRIVATE`, `LTD`, `LIMITED`, `LLP`, `INC`, `CO`, `COMPANY`, `AND`, `&`
5. Compare with `rapidfuzz.token_set_ratio`, threshold **85**

Check the vendor master `aliases` list too, and take the best score across all aliases.

### 9.3 Matching output

```json
{
  "match_status": "MATCHED",
  "match_layer": 1,
  "match_confidence": "HIGH",
  "po_number": "PO-1010",
  "candidates_considered": [
    {"po_number": "PO-1010", "score": 100, "reason": "Explicit PO reference on invoice"}
  ],
  "vendor_match_score": 96,
  "notes": []
}
```

Always record `candidates_considered`, even when there was only one. The trace should show what the system *looked at*, not just what it picked.

---

## 10. Rules catalogue

Every rule is a separate function with the same signature. Every rule returns the same shape:

```python
@dataclass
class RuleResult:
    rule_id: str
    name: str
    status: str        # PASS | FAIL | WARN | SKIP
    severity: str      # BLOCKER | CRITICAL | WARNING | INFO
    expected: Any
    actual: Any
    message: str       # plain English, written for a human reader
```

**Run the entire rule set every time. Never short-circuit.** Even after a blocker fails, keep going. A reviewer wants to see the full picture, not just the first thing that broke. A rule that could not run (because a prerequisite was missing) returns `SKIP` with a reason — it does not crash and does not silently pass.

### 10.1 Severity meanings

| Severity | Meaning | Effect on decision |
|---|---|---|
| **BLOCKER** | Structurally impossible to approve. No amount of context fixes it. | → `REJECT` |
| **CRITICAL** | Something is genuinely wrong or unclear. A human must look. | → `HOLD_FOR_REVIEW` |
| **WARNING** | Financially fine, but worth noting for audit. | → `APPROVE_WITH_FLAG` |
| **INFO** | Recorded for the trace. Never changes the outcome. | none |

### 10.2 Completeness & integrity rules (R-0xx)

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-001 | Invoice number present | CRITICAL | Value is null |
| R-002 | Invoice date present and parseable | CRITICAL | Null, unparseable, or impossible date |
| R-003 | Subtotal present | CRITICAL | Null |
| R-004 | Total present | CRITICAL | Null |
| R-005 | Vendor identifiable | BLOCKER | No vendor name and no GSTIN |
| R-006 | Line items sum to subtotal | WARNING | Difference > ₹1 |
| R-007 | Subtotal + tax = total | CRITICAL | Difference > ₹1 |
| R-008 | Critical fields meet confidence floor | WARNING | Any critical field below 0.80 |
| R-009 | Supporting fields meet confidence floor | INFO | Any supporting field below 0.70 |

Note R-006 vs R-007. If line items do not sum but the subtotal-plus-tax does, it is probably an extraction miss on one line — a warning. If the invoice's own headline maths is wrong, that is a real problem — critical.

### 10.3 Matching rules (R-1xx)

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-101 | A PO was matched | BLOCKER | `match_status = NO_MATCH` |
| R-102 | Match is unambiguous | CRITICAL | `match_status = AMBIGUOUS` |
| R-103 | Invoice vendor matches PO vendor | BLOCKER | Fuzzy score < 85 |
| R-104 | PO is open | BLOCKER | Status is `CLOSED` or `CANCELLED` |
| R-105 | Match was made on explicit reference | WARNING | Matched via Layer 2 inference |

R-105 is worth having. A PO matched by inference rather than by a printed reference is still probably right — but a reviewer should know the system guessed.

### 10.4 Vendor rules (R-2xx)

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-201 | Vendor is on the approved list | BLOCKER | `is_approved = False` |
| R-202 | Invoice GSTIN matches vendor master GSTIN | CRITICAL | Both present and different |
| R-203 | Not the vendor's first invoice | WARNING | No prior accepted invoice on record |

### 10.5 Financial rules (R-3xx) — the core

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-301 | Currency matches PO currency | BLOCKER | Mismatch |
| R-302 | **Overage within tolerance** | CRITICAL | `overage > min(1.5% × po_total, ₹10,000)` |
| R-303 | Cumulative invoiced within PO + tolerance | CRITICAL | `(already_invoiced + subtotal) > po_total + allowed_overage` |
| R-304 | Tolerance consumption under 50% | WARNING | Overage used > half the allowance |
| R-305 | Under-billing observed | INFO | Subtotal < remaining balance — **never fails** |

R-302 and R-303 look similar but catch different things. R-302 asks "is *this* invoice too big for what's left?" R-303 asks "would accepting this push the *contract total* past its ceiling?" With correct arithmetic they usually agree — but keeping both means a bug in one is caught by the other.

R-305 exists purely so under-billing shows up in the trace as a deliberate, recorded observation rather than silence. A reviewer reading the trace should be able to tell the difference between "we checked and it was under" and "we never looked."

### 10.6 Tax rules (R-4xx)

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-401 | GST rate is a valid slab | CRITICAL | Not in {0, 5, 12, 18, 28} |
| R-402 | Tax arithmetic correct | CRITICAL | `subtotal × rate ≠ tax`, beyond ₹1 |
| R-403 | GSTIN present and 15 characters | WARNING | Missing or wrong length |
| R-404 | GST rate matches PO expectation | WARNING | Differs from `expected_gst_rate` |

### 10.7 Duplicate rules (R-5xx)

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-501 | Not an exact duplicate | BLOCKER | Same vendor + same invoice number already processed |
| R-502 | Not a suspected near-duplicate | CRITICAL | Same vendor + same amount within 30 days, different invoice number, **and** service periods do not clearly differ |

R-502 is the interesting one and is built out fully in EC-2 below.

### 10.8 Date rules (R-6xx)

| ID | Rule | Severity | Fails when |
|---|---|---|---|
| R-601 | Invoice date is not before PO date | CRITICAL | Invoice predates the PO |
| R-602 | Invoice date is not in the future | CRITICAL | Later than today |
| R-603 | Invoice date within PO validity | WARNING | After `valid_until` |
| R-604 | Date was unambiguous | WARNING | Format could be read as DD/MM or MM/DD |

R-601 is a genuine fraud signal. An invoice dated before the PO that authorised it means either back-dating or a purchase made without approval.

---

## 11. Decision matrix

The decision function takes the full list of `RuleResult` objects and applies one rule:

> **Highest severity among all failures wins.**

```
if any rule with severity BLOCKER has status FAIL   ->  REJECT
elif any rule with severity CRITICAL has status FAIL ->  HOLD_FOR_REVIEW
elif any rule with severity WARNING has status FAIL  ->  APPROVE_WITH_FLAG
else                                                 ->  AUTO_APPROVE
```

That is the entire decision engine. It should be about eight lines of code. If it grows beyond twenty, complexity has leaked out of the rules and into the decision layer — push it back.

### 11.1 What each decision means to the business

| Decision | Meaning | What happens next |
|---|---|---|
| `AUTO_APPROVE` | Clean. Everything matched. | Queue for payment. No human touch. |
| `APPROVE_WITH_FLAG` | Financially sound, but something is worth recording. | Pays automatically; appears on an audit report. |
| `HOLD_FOR_REVIEW` | Something is wrong or unclear. Could still be legitimate. | Goes to an AP reviewer with the specific issue named. |
| `REJECT` | Cannot be approved by this process under any reading. | Returned to vendor or escalated. |

### 11.2 Why no amount ever causes an automatic REJECT

A deliberate design position, and worth stating in your README because it looks like an omission otherwise.

An invoice 40% over its PO might be fraud — or it might be an agreed scope change nobody updated the PO for. **The system cannot tell the difference, so it must not pretend to.** It holds, names the discrepancy, and lets a human decide.

Rejection is reserved for things that are true regardless of context: no PO exists, the vendor is not approved, the PO is closed, this exact invoice was already processed. No amount of business context makes those approvable.

---

## 12. Edge cases

Four edge cases. Each needs **generalisable logic**, not a special case.

> If you ever write `if invoice_id == "INV-042"`, stop. The general rule is the thing being assessed.

### EC-1 · Progressive billing against one PO

**Scenario.** A ₹10,00,000 PO for a phased consulting engagement. The vendor bills three times: ₹4,00,000, then ₹3,50,000, then ₹2,60,000. Each individual invoice is far below the PO. Naively, each looks like a harmless under-delivery.

**Why it is hard.** A system comparing each invoice against the *full PO total* approves all three and pays ₹10,10,000 against a ₹10,00,000 commitment without ever noticing. The overspend is invisible at the level of any single invoice.

**Required behaviour.** Tolerance is evaluated against **remaining balance**, and `already_invoiced_paise` is updated whenever an invoice is accepted. The third invoice is then correctly seen as overage of exactly ₹10,000 — on the boundary, so it passes, but consumes 100% of the allowance and triggers R-304.

**Expected:** A → `AUTO_APPROVE`, B → `AUTO_APPROVE`, C → `APPROVE_WITH_FLAG`

**What it demonstrates.** State awareness. The system understands a PO as a running balance, not a static number. Call out in your README that this required *no special-case code* — the right rule design made it fall out naturally.

**Also build the failure twin:** make invoice C ₹2,75,000 instead. Overage becomes ₹25,000, exceeding the ₹10,000 allowance → `HOLD_FOR_REVIEW`. Same logic, different outcome, no code change.

### EC-2 · Recurring billing that looks like a duplicate

**Scenario.** Sharma Logistics has a monthly freight retainer. Two invoices arrive:

- `INV-2201`, 16 March 2026, ₹45,000, line item: "Freight services — 01 Mar to 15 Mar"
- `INV-2209`, 31 March 2026, ₹45,000, line item: "Freight services — 16 Mar to 31 Mar"

Same vendor. Same amount. Same PO. 15 days apart. Different invoice numbers.

**Why it is hard.** Every naive duplicate heuristic — same vendor, same amount, close dates — fires on this. Auto-rejecting means refusing to pay a vendor for work they did. Auto-approving means a real duplicate would sail through the same gap. **The amount and date tell you nothing here.** The distinguishing signal is buried in the line item text.

**Required behaviour.** When R-502 detects a near-duplicate candidate, the system escalates to a second check: compare extracted `service_period` values.

- Periods present and non-overlapping → not a duplicate → `PASS`
- Periods present and overlapping → genuine duplicate suspicion → `FAIL`, critical
- Period absent on either invoice → cannot determine → `FAIL`, critical, with the message "possible duplicate; unable to confirm because no service period was found"

**Expected:** INV-2201 → `AUTO_APPROVE`; INV-2209 → `AUTO_APPROVE` (periods differ). Then a third test invoice, `INV-2214` for ₹45,000 covering "16 Mar to 31 Mar" again → `HOLD_FOR_REVIEW`.

**What it demonstrates.** That you extracted a field a naive implementation would never have thought to extract, because you reasoned about what actually distinguishes the two cases. This is the strongest signal of understanding in the whole set.

### EC-3 · The tolerance cap binds where the percentage would not

**Scenario.** PO-1005, ₹20,00,000. Invoice arrives for ₹20,25,000. Overage ₹25,000, which is **1.25%** of the PO.

**Why it is hard.** 1.25% is comfortably under the stated 1.5% threshold. A single-threshold implementation approves it — and pays ₹25,000 more than committed. But the policy is `min(1.5%, ₹10,000)`, and on a PO this large the rupee cap is the binding constraint.

**Required behaviour.** `allowed = min(₹30,000, ₹10,000) = ₹10,000`. Overage of ₹25,000 exceeds it. R-302 fails, critical.

**Expected:** `HOLD_FOR_REVIEW`, with a message naming *which* limit bound: "Overage ₹25,000 exceeds allowance of ₹10,000 (absolute cap; 1.5% would have permitted ₹30,000)."

**What it demonstrates.** Correct implementation of a dual-threshold policy, and — through that message — a trace that explains not just that a limit was hit but *which* limit and why. Pair this with a small-PO invoice where the percentage binds instead, so both branches are visibly exercised.

### EC-4 · Missing invoice number on a poor-quality scan

**Scenario.** A scanned, slightly rotated, low-DPI invoice. The header where the invoice number sits is faint. The vendor name and totals are legible; the invoice number is not.

**Why it is hard.** This is where a language model is most likely to be *helpfully wrong* — inventing something that looks like a plausible invoice number. A fabricated invoice number is worse than a missing one: it silently defeats duplicate detection forever, because no future invoice will ever match it.

**Required behaviour.**
1. Extraction returns `null` with an explicit reason. It does **not** guess.
2. R-001 fails (critical) — the field is required.
3. R-501 (exact duplicate) returns `SKIP`, not `PASS`, with the reason "cannot check for duplicates without an invoice number." **The distinction between SKIP and PASS is the whole point.** A skipped check must never read as a clean check.
4. Every other rule still runs. The trace shows what *was* successfully read.
5. The output tells the reviewer precisely what is needed: "Could not read invoice number. All other fields extracted successfully (vendor matched PO-1003, amount within tolerance). Please supply the invoice number to complete processing."

**Expected:** `HOLD_FOR_REVIEW`

**What it demonstrates.** Graceful degradation, and honest handling of uncertainty. The system fails usefully — partial results plus a specific ask — rather than crashing or fabricating. Make sure you also demo the same invoice as a **clean PDF**, where it processes normally: same document, different quality, different behaviour.

### Optional fifth (only if time permits)

Vendor invoices with GST at 12% where the PO expects 18%, and the total happens to still land within tolerance. Tests whether tax validation is genuinely independent of amount validation. Skip this if it costs you polish elsewhere — four well-built edge cases beat five rushed ones.

---

## 13. The audit trace

The trace is the actual product. Design it first, not last.

```json
{
  "trace_id": "uuid",
  "processed_at": "2026-08-09T14:22:01Z",
  "source_file": "data/invoices/INV-2209.pdf",
  "source_type": "scanned_image",
  "pipeline_version": "1.0",

  "stage_1_extraction": {
    "model": "claude-sonnet-4-6",
    "duration_ms": 2140,
    "fields": { "...full extracted schema..." },
    "self_checks": {
      "line_items_sum_to_subtotal": true,
      "subtotal_plus_tax_equals_total": true,
      "tax_matches_rate": true
    },
    "low_confidence_fields": ["vendor_gstin"]
  },

  "stage_2_matching": {
    "match_status": "MATCHED",
    "match_layer": 1,
    "po_number": "PO-1010",
    "candidates_considered": [ "..." ],
    "vendor_match_score": 96
  },

  "stage_3_financials": {
    "po_total_paise": 100000000,
    "already_invoiced_paise": 75000000,
    "remaining_balance_paise": 25000000,
    "invoice_subtotal_paise": 26000000,
    "overage_paise": 1000000,
    "allowed_overage_paise": 1000000,
    "binding_constraint": "absolute_cap",
    "tolerance_consumption_pct": 100.0
  },

  "stage_4_rules": [
    {"rule_id": "R-001", "name": "Invoice number present",
     "status": "PASS", "severity": "CRITICAL",
     "expected": "not null", "actual": "INV-2209",
     "message": "Invoice number found."},
    {"rule_id": "R-304", "name": "Tolerance consumption under 50%",
     "status": "FAIL", "severity": "WARNING",
     "expected": "<= 50%", "actual": "100.0%",
     "message": "This invoice consumes the entire remaining tolerance allowance for PO-1010."}
  ],

  "stage_5_decision": {
    "decision": "APPROVE_WITH_FLAG",
    "determined_by": ["R-304"],
    "rules_run": 31,
    "rules_passed": 29,
    "rules_failed": 1,
    "rules_skipped": 1,
    "summary": "Approved with a flag. The invoice matches PO-1010 and the ₹10,000 overage sits exactly on the permitted limit, but it uses the full remaining tolerance for this PO — any further billing will breach it."
  }
}
```

Three things to get right:

- **`determined_by`** names the specific rule(s) that produced the outcome. Not the whole list — the deciding ones. This is what makes the system explainable in one glance.
- **`binding_constraint`** records whether the percentage or the cap bound. Small field, disproportionate credibility.
- **`summary`** is the one place a model is used post-decision, and only to phrase a conclusion already reached deterministically. Pass it the decision and the failing rules; ask for two or three sentences a finance person would understand. If the API is unavailable, fall back to a template — the system must work without it.

---

## 14. Test corpus and answer key

**Build the answer key at the same time as the invoices — not afterwards.**

The reason is uncomfortable but real: if you build the engine first and the test data second, you will unconsciously create data your engine already handles. The answer key written in advance is the only honest test you have.

### 14.1 PO master — 15 rows

| Purpose | Count | Notes |
|---|---|---|
| Standard open POs, small (< ₹6,66,667) | 5 | Percentage binds |
| Standard open POs, large (> ₹6,66,667) | 4 | Cap binds |
| Progressive-billing PO (₹10,00,000) | 1 | For EC-1 |
| Monthly retainer PO | 1 | For EC-2 |
| Closed PO | 1 | Triggers R-104 |
| Cancelled PO | 1 | Triggers R-104 |
| PO for an unapproved vendor | 1 | Triggers R-201 |
| PO with past `valid_until` | 1 | Triggers R-603 |

### 14.2 Invoices — 20 documents

| Group | Count | Covers |
|---|---|---|
| Clean happy path, varied layouts | 6 | Baseline. At least 4 visually distinct vendor templates. |
| Scanned/rotated versions of clean invoices | 3 | Proves the vision path works |
| EC-1 progressive billing | 4 | Three passing + one failure twin |
| EC-2 recurring vs duplicate | 3 | Two legitimate + one true duplicate |
| EC-3 cap-binding breach | 1 | Plus one small-PO percentage breach |
| EC-4 unreadable invoice number | 1 | Scanned, degraded |
| Assorted rule triggers | 2 | Unapproved vendor, invoice predating PO |

Generate PDFs from HTML templates — four or five different layouts with different label wording ("Invoice No." / "Bill Number" / "Doc Ref"), different table structures, tax embedded vs separate. Layout variety is what makes the extraction result meaningful.

Produce scanned versions by rendering to PNG at ~110 DPI, rotating 0.5–2°, adding light gaussian noise, then wrapping back into a PDF.

### 14.3 Answer key format

`data/answer_key.csv`:

| Column | Example |
|---|---|
| `invoice_file` | `INV-2209.pdf` |
| `expected_decision` | `APPROVE_WITH_FLAG` |
| `expected_po` | `PO-1010` |
| `expected_determining_rules` | `R-304` |
| `expected_subtotal_paise` | `26000000` |
| `notes` | `EC-1 invoice C — lands exactly on tolerance boundary` |

---

## 15. Build phases

Each phase has a **done-when** you can actually check. Do not start the next phase until the current one passes.

### Phase 0 — Lock the spec ✅
This document. Done.

### Phase 1 — Skeleton and config
Repo structure, `config.py` with every constant from Section 5, `schemas.py` with Pydantic models from Section 7, empty module files.
**Done when:** `python -c "import src.config, src.schemas"` runs clean.

### Phase 2 — PO and vendor master
Build both CSVs per Section 14.1. Loader into SQLite. Functions: `get_po()`, `get_vendor()`, `update_already_invoiced()`.
**Done when:** you can query any PO and read its remaining balance.

### Phase 3 — Invoice corpus + answer key
HTML templates → PDFs → scanned variants. Answer key written **as you go**.
**Done when:** 20 PDFs exist and every one has a row in `answer_key.csv`.

### Phase 4 — Extraction
Vision call, strict schema validation, on-disk cache keyed by file hash, self-checks.
**Done when:** all 20 extract without schema errors, and you have manually eyeballed 5 against the source PDFs.

### Phase 5 — Matching
Layers 1–4, vendor normalisation, ranked candidates.
**Done when:** every invoice either resolves to a PO with a stated confidence, or returns a written reason for no match.

### Phase 6 — Rules
All rules from Section 10, one function each, in `rules.py`. Unit tests per rule with hand-made inputs — do not test rules through the full pipeline.
**Done when:** `pytest` passes with at least one test per rule.

### Phase 7 — Decision + trace
The eight-line decision function. Full trace assembly. Write traces to `outputs/traces/`.
**Done when:** every invoice produces a complete, valid trace JSON.

### Phase 8 — Edge cases
Wire in EC-1 through EC-4. Most should already work if Phases 5–6 were built properly. What is left is EC-2's service-period comparison and EC-4's SKIP semantics.
**Done when:** all four behave as Section 12 specifies.

### Phase 9 — Interface
Streamlit. Three views: upload/process, decision detail with expandable trace, queue grouped by decision.
**Deliberately last.** Building the UI early is the most reliable way to waste a weekend — you rewire it every time the data model shifts.
**Done when:** you can process an invoice and read its full trace without touching a terminal.

### Phase 10 — Validation
Run all 20, compare to the answer key, report accuracy per stage.
**Done when:** you have a printed scorecard and an explanation for every mismatch.

### Phase 11 — Package
README, architecture reasoning, rules table, edge case write-ups, honest limitations. Short walkthrough recording if possible.
**Done when:** someone who has never seen the project can run it from the README alone.

---

## 16. Repository structure

```
invoice-decisioning/
├── README.md
├── requirements.txt
├── spec/
│   └── PS1_Invoice_Decisioning_Spec.md   ← this document
├── data/
│   ├── po_master.csv
│   ├── vendor_master.csv
│   ├── answer_key.csv
│   └── invoices/
│       ├── clean/
│       └── scanned/
├── src/
│   ├── config.py       # every constant, nothing hard-coded elsewhere
│   ├── schemas.py      # Pydantic models
│   ├── store.py        # PO/vendor data access
│   ├── extract.py      # vision call + cache + self-checks
│   ├── match.py        # matching layers
│   ├── rules.py        # one function per rule
│   ├── decide.py       # severity resolution
│   ├── trace.py        # trace assembly + summary
│   └── pipeline.py     # orchestration
├── scripts/
│   ├── generate_invoices.py
│   ├── degrade_to_scan.py
│   └── run_batch.py
├── app/
│   └── streamlit_app.py
├── tests/
│   ├── test_rules.py
│   ├── test_tolerance.py    # boundary cases from Section 6.3
│   └── test_matching.py
└── outputs/
    ├── traces/
    └── scorecard.md
```

**Suggested stack:** Python 3.11 · Pydantic v2 · SQLite · rapidfuzz · PyMuPDF · Jinja2 + WeasyPrint · Pillow · Streamlit · pytest.

---

## 17. Validation and metrics

Report four numbers in `outputs/scorecard.md`:

1. **Field extraction accuracy** — per critical field, across all 20 invoices. Expect 95%+ on clean PDFs, lower on scans. Report both separately; the gap is informative.
2. **Match accuracy** — did it find the right PO? Report by layer.
3. **Decision accuracy** — a 4×4 confusion matrix of expected vs actual across the four decisions.
4. **Explanation correctness** — did `determined_by` name the rule you expected? This one matters most and almost nobody measures it.

On mismatches, ask honestly: **is the engine wrong, or was my expectation wrong?** Sometimes the engine is right and the answer key was careless. Document which, either way — a corrected answer key with a reason is a sign of rigour, not sloppiness.

**Errors that matter most, in order:**
1. Something rejected that should have been approved (blocks a legitimate vendor payment)
2. Something auto-approved that should have been held (money leaves without review)
3. Right decision, wrong stated reason (the explanation is the product)

---

## 18. Out of scope — say this explicitly

Listing what you deliberately did not build is a strength. It shows you knew the boundary and chose it.

| Not built | Why | How you would extend |
|---|---|---|
| **3-way matching** (invoice vs PO vs goods receipt) | No GRN data available | Add a `grn` table; new rule R-106 comparing invoiced qty to received qty. The architecture supports it — it is one more rule and one more data source. |
| **Multi-PO invoices** | Doubles matching complexity | Allocate line items across POs, then run tolerance per PO. |
| **Multi-currency** | No FX rate source | Rate table keyed by date; convert to base currency before comparison. |
| **Concurrent processing** | Single-user by design | `already_invoiced_paise` needs row-level locking or optimistic versioning. |
| **Learning from reviewer decisions** | Needs history we do not have | Log reviewer overrides; use them to tune thresholds over time. |
| **Contract/rate-card validation** | No contract data | Compare unit prices against an agreed rate card. |
| **Vendor bank verification** | Belongs to PS-2 | — |

---

## 19. Open questions to confirm before Phase 4

Raise these with your manager. Getting a real answer to even two of them improves the build noticeably — and asking them is itself a good signal.

| # | Question | Current working answer |
|---|---|---|
| Q-01 | Does the ₹10,000 cap apply per invoice or per PO lifetime? | **Per PO**, checked against remaining balance. Per-invoice would let a vendor split into ten invoices for ten allowances. |
| Q-02 | Should under-billing ever be flagged? | **No.** Recorded as INFO only. Partial delivery is normal. |
| Q-03 | Is the tolerance on pre-tax or tax-inclusive value? | **Pre-tax.** See Section 4.1. This is the assumption most worth confirming. |
| Q-04 | Do we ever auto-reject on amount alone? | **No.** Section 11.2. |
| Q-05 | Should a PO past `valid_until` block or flag? | **Flag** (R-603). Late invoicing against a valid PO is common. |
| Q-06 | Who reviews `HOLD_FOR_REVIEW`, and within what SLA? | Not modelled. Would matter in production. |
| Q-07 | Does `APPROVE_WITH_FLAG` actually pay, or wait? | Assumed **pays**, flagged for later audit. |
| Q-08 | Is a credit note in scope? | **No** in v1 — but worth naming, since a negative-amount invoice would currently confuse the tolerance logic. Guard against negative subtotals. |

Q-08 is worth a defensive check in code even though credit notes are out of scope. A negative subtotal should be caught and held, not silently processed.

---

## 20. The three ways this goes wrong

Watch for these specifically.

**1. Letting the model make the decision because it "seems smarter."**
It is tempting. It reads well in a demo. It destroys reproducibility, and reproducibility is the entire point of the brief. Extract with the model. Decide with Python. No exceptions.

**2. Writing test data after building the engine.**
You will unconsciously build an engine that passes the data you invent for it, then be surprised when a real invoice arrives. Answer key first — Phase 3 comes before Phase 4 for exactly this reason.

**3. Special-casing edge cases.**
The moment an edge case is handled with an `if` on a specific invoice, it has stopped being a demonstration of judgement and become a hard-coded exception. Every edge case in Section 12 has a general rule behind it. Find the rule.

---

## 21. What to emphasise when presenting

The brief says: *"with everything that happened in between visible."* That sentence is the assignment.

Lead with the trace, not the extraction. Open a `HOLD_FOR_REVIEW` decision and walk through it: here is what was read and how confident we were, here is the PO we matched and the alternatives we considered, here are all thirty-one rules that ran, and here is the one that decided the outcome.

Then show EC-1 and point out that progressive billing required no special code — the right rule design handled it. Then show EC-4 and point out that a skipped duplicate check reports as `SKIP`, never as `PASS`, because a check that could not run must never look like a check that succeeded.

Those two points, made plainly, say more about engineering judgement than any amount of feature count.
