# Invoice Decisioning (PS-1)

Takes a vendor invoice, reads it, matches it to a purchase order, runs 33
independent checks, and produces one of four decisions — with a complete audit
trace showing every field it read, every option it considered, and the exact
check that decided the outcome.

## The principle everything rests on

> **The AI reads. The code decides.**

A model is used for exactly one thing: turning an invoice into structured
numbers. Every judgement after that is deterministic Python. The same invoice
always produces the same decision, and any outcome traces back to a specific
named rule. In a payments system, an approval that cannot be reproduced is not
an approval — it is a guess.

## Quick start

```
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt

python -m scripts.load_masters     # build the database from the CSVs
python -m scripts.run_batch        # process all 21 invoices
streamlit run app/streamlit_app.py # the interface
```

Run everything from the project root. The free reader needs no API key.

## What you can run

| Command | What it does |
|---|---|
| `python -m scripts.load_masters` | Rebuild the database from `data/*.csv` |
| `python -m scripts.generate_invoices` | Regenerate the 21 invoice PDFs |
| `python -m scripts.degrade_to_scan` | Turn 3 of them into realistic scans |
| `python -m scripts.verify_corpus` | Nine integrity checks over the test set |
| `python -m scripts.run_batch` | Process everything, score against the answer key |
| `python -m scripts.demo_edge_cases` | Walk through the four edge cases with commentary |
| `python -m scripts.validate` | Write `outputs/scorecard.md` |
| `python -m scripts.compare_tiers` | Free vs premium reader, side by side |
| `python -m pytest tests/ -q` | 68 unit tests |

## The four decisions

| Decision | Meaning |
|---|---|
| `AUTO_APPROVE` | Clean. Straight to payment. |
| `APPROVE_WITH_FLAG` | Financially sound, recorded for audit. |
| `HOLD_FOR_REVIEW` | Wrong or unclear. A person must look. |
| `REJECT` | Cannot be paid as submitted, regardless of amount. |

**No amount ever causes an automatic rejection.** An invoice 40% over its PO
might be fraud or an agreed change nobody wrote down — the system cannot tell,
so it holds and asks. Rejection is reserved for things wrong regardless of
context: no PO, an unapproved supplier, a closed order, a confirmed duplicate.

## The tolerance rule

```
allowed_overage = min( 1.5% of the PO total , Rs 10,000 )
```

Measured against the **remaining balance**, not the PO total — which is what
makes progressive billing work without any special-case code, and what stops a
supplier splitting one order into several invoices to collect a fresh allowance
each time. Compared on **pre-tax** values; GST is validated separately.

The two limits cross at a PO of **Rs 6,66,666.66**. Below that the percentage
binds; above it, the cash cap. An invoice can be under one limit and over the
other — exactly the case that catches out a single-threshold implementation.

## Two readers

| | Free | Premium |
|---|---|---|
| How | Reads the text layer digital PDFs already carry | Vision model via Chat Completions |
| Cost | Nothing | A fraction of a rupee per page |
| Scans | Refuses them, and says why | Handles them |
| Needs a key | No | Yes — entered in the app, or in `.env` |

Both return the identical shape, so matching, rules, decisions and traces are
tier-agnostic. Only extraction quality differs — which is the whole point: the
same rule set degrades honestly rather than guessing.

### Getting a key into the premium reader

Select **Premium** in the sidebar and a panel appears. Two routes:

**OpenAI direct** — your own key, straight to `api.openai.com`.

**Portkey gateway** — a gateway key plus one of a provider, a virtual key, or
a saved config id. The provider's own credential stays in Portkey, so the
person running this only ever holds a gateway key. Point **Gateway URL** at
your own host if your organisation runs one.

**Test the connection** sends one tiny request, so a key can be checked without
spending a page of image tokens on an invoice.

Nothing typed into the app is written to disk. It lives for the session and
then goes away. Keys never reach a trace, a log line, or an error message —
`Credentials.redacted()` is the only thing that can be printed.

Scripts read from `.env` instead; see `.env.example` for both routes.

**Chat Completions rather than the newer Responses API** is deliberate: it is
what every OpenAI-compatible gateway supports, Portkey included, whose own docs
map Responses calls back onto Chat. One code path serves both routes rather
than branching on where the request is going.

## Architecture

```
invoice PDF -> extract -> match to PO -> run rules -> decide -> write trace
```

| Module | Role |
|---|---|
| `src/config.py` | Every tunable constant. Nothing hard-coded elsewhere. |
| `src/money.py` | Integer-paise arithmetic and the tolerance engine. |
| `src/schemas.py` | Data contracts for every stage. |
| `src/store.py` | The only module that touches the database. |
| `src/prompts.py` | The extraction prompt. |
| `src/extract.py` | Cache, text detection, quality gate, tier dispatch. |
| `src/extract_local.py` | Free reader — PyMuPDF text + label matching. |
| `src/credentials.py` | Runtime credentials. OpenAI or Portkey. Never persisted. |
| `src/extract_vision.py` | Premium reader — vision model, strict JSON schema. |
| `src/match.py` | Invoice to PO matching, four layers. |
| `src/rules.py` | 33 rules, one function each. |
| `src/decide.py` | Severity resolution. 44 lines. |
| `src/trace.py` | Audit trace assembly. |
| `src/pipeline.py` | Sequences the stages. No business logic. |
| `app/streamlit_app.py` | Interface. No business logic either. |

## Three invariants in the rule engine

1. **Every rule runs on every invoice.** Nothing short-circuits. Even after a
   blocker fails, the rest still run — a reviewer wants the whole picture.
2. **A rule that cannot run reports SKIP, never PASS.** A check that did not
   happen must never look like one that succeeded.
3. **Rules do not know about decisions.** Each states its own severity;
   `decide.py` resolves the collection. All the judgement is in the rules.

## The four edge cases

Run `python -m scripts.demo_edge_cases` to see these live. **None required
special-case code** — every one falls out of a general rule.

**EC-1 · Progressive billing.** Three invoices against one Rs 10,00,000 PO.
Each looks like a modest under-delivery on its own; together they exceed the
commitment. The third lands on exactly Rs 10,000 overage against exactly
Rs 10,000 allowance and passes, because the rule uses `>` not `>=`.

**EC-2 · Recurring billing vs a duplicate.** Three invoices, same supplier,
same Rs 45,000, days apart, different numbers. Every naive duplicate heuristic
fires on all three. The only separating signal is the **service period** — which
is why the extractor goes looking for a field a naive implementation would never
think to extract.

**EC-3 · Which tolerance limit binds.** One invoice breaches at 1.25% of its PO
(under the 1.5% threshold, over the cash cap). Another breaches at Rs 5,000
(under the cap, over the percentage). Each passes the *other* limit. A
single-threshold system gets one of them wrong.

**EC-4 · A field that cannot be read.** A degraded scan where the invoice number
is destroyed. The reader returns null with a reason rather than inventing one —
a fabricated invoice number would defeat duplicate detection forever. The
duplicate check reports **SKIP**, never PASS.

## Results

| Metric | Free tier |
|---|---|
| Extraction accuracy | 144/144 (100%) |
| Match accuracy | 18/18 (100%) |
| Decision accuracy | 21/21 (100%) |
| **Reason accuracy** | **21/21 (100%)** |

Reason accuracy — did it cite the *right rule* — is the metric that matters most
and the one almost nobody measures. A right decision for the wrong reason is not
correct; it is lucky.

`outputs/scorecard.md` includes a candid section on what these numbers do and do
not prove: extraction is exact by construction on the free tier, and the corpus
is synthetic and therefore cannot surprise the system. The answer key was
written in Phase 3, before any extraction, matching or rule code existed.

## Known limitations

- **2-way matching only** (invoice vs PO). No goods-receipt data, so no 3-way
  match. The architecture supports it: one more table, one more rule.
- **One invoice, one PO.** Multi-PO invoices are real but out of scope.
- **Single currency (INR).** A currency-mismatch rule guards the assumption.
- **Single-user.** `already_invoiced` would need row locking for concurrency.
- **Layer 3 matching is not exercised by the corpus.** No two POs fall inside
  the ±10% window together, so it is covered by unit test instead.
- **Credit notes are out of scope**, but a negative subtotal raises rather than
  being silently processed as extreme under-billing.
- **The premium reader's live call has not been exercised end to end.** The
  schema, conversion, rendering, credential handling and every error path are
  tested, but the round trip needs a real key and outbound network access.

## Repository layout

```
data/           PO master, vendor master, seed history, answer key, invoice PDFs
src/            The engine
scripts/        Corpus generation, batch runs, validation, edge-case demo
app/            Streamlit interface
tests/          68 unit tests
spec/           The full specification written before any code
outputs/        Traces and the scorecard (generated)
```
