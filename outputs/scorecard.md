# Validation scorecard

Generated 2026-08-16 13:45 UTC  
Corpus: 21 invoices  
Rules: 33  
Batch date fixed at 2026-08-10 so future-date checks are reproducible


## Free tier

### 1. Extraction accuracy

144/144 field values correct (100.0%). 3 invoice(s) excluded — the quality gate stopped them, so nothing was claimed and there is nothing to score.

| Field | Correct | Checked | Accuracy |
|---|---|---|---|
| `invoice_number` | 18 | 18 | 100.0% |
| `invoice_date` | 18 | 18 | 100.0% |
| `vendor_gstin` | 18 | 18 | 100.0% |
| `po_reference` | 18 | 18 | 100.0% |
| `subtotal_paise` | 18 | 18 | 100.0% |
| `gst_rate` | 18 | 18 | 100.0% |
| `tax_paise` | 18 | 18 | 100.0% |
| `total_paise` | 18 | 18 | 100.0% |

### 2. Match accuracy

18/18 matched to the correct purchase order (100.0%). 3 gated.

| Layer | How | Count |
|---|---|---|
| 1 | explicit PO reference printed on the invoice | 18 |

### 3. Decision accuracy

**21/21 (100.0%)**

Rows are expected, columns are actual.

| expected \ actual | Auto Approve | Approve With Flag | Hold For Review | Reject |
|---|---|---|---|---|
| Auto Approve | **9** | · | · | · |
| Approve With Flag | · | **2** | · | · |
| Hold For Review | · | · | **7** | · |
| Reject | · | · | · | **3** |

### 4. Reason accuracy

**21/21 (100.0%)** — of the 21 correct decisions, 21 also cited exactly the expected rule(s).

> A right decision for the wrong reason is not correct. It is lucky, and it will be wrong on the next invoice.


## Summary

| Metric | free |
|---|---|
| Extraction | 144/144 (100.0%) |
| Match | 18/18 (100.0%) |
| Decision | 21/21 (100.0%) |
| Reason | 21/21 (100.0%) |

## How to read these numbers

High scores here are less impressive than they look, and it is worth being straight about why.

**Extraction is exact by construction on the free tier.** It reads the text layer a born-digital PDF already contains -- the literal characters, not a guess at pixels. Anything other than 100% would mean a label-matching bug, not a recognition failure. The premium tier is the one where this metric earns its keep.

**The corpus is synthetic.** These invoices were generated for this project, so they cannot surprise it. Four visually distinct templates with different label wording and a CGST/SGST split give real variety, but they are still variety that was anticipated. A corpus of genuine vendor invoices would be a far harder test.

**Decision and reason accuracy are the meaningful figures.** The answer key was written in Phase 3, before any extraction, matching or rule code existed. Those expectations were not adjusted to fit the engine -- with one documented exception: three rows were updated when R-303 was implemented as a cross-check of R-302 and correctly began failing alongside it.

**What would genuinely test this:** real invoices from real vendors, a PO master someone else built, and an answer key written by someone who had not seen the rules.

## What is not measured

- **Layer 3 matching (ambiguous).** No two POs in the master fall inside the ±10% amount window together, so the path is covered by unit test rather than by corpus.
- **EC-4's invoice number.** Excluded from extraction scoring: the scan destroyed it, so a null is the correct answer and scoring it would reward a hallucination.
- **Gated invoices.** Excluded from extraction and match scoring. Nothing was claimed, so there is nothing to be right or wrong about. The gate outcome is itself scored under decisions.
