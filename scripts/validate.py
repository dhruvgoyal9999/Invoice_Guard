"""
Score the pipeline against the answer key and write outputs/scorecard.md.

    python -m scripts.validate                  free tier
    python -m scripts.validate --tier premium   needs an API key
    python -m scripts.validate --tier both

Four metrics, in ascending order of how much they matter:

  1. Extraction accuracy   did it read the right values
  2. Match accuracy        did it find the right PO
  3. Decision accuracy     did it reach the right outcome
  4. REASON accuracy       did it cite the right rule

The fourth is the one that matters most and the one almost nobody measures. A
system that reaches the right decision for the wrong reason is not correct --
it is lucky, and it will be wrong on the next invoice.
"""

import argparse
import csv
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

from src import config, store
from src.money import rupees_to_paise as R
from src.pipeline import process_invoice
from src.schemas import Decision, MatchStatus, Tier
from scripts.invoice_specs import INVOICES

BATCH_DATE = date(2026, 8, 10)
SPECS = {s["stem"]: s for s in INVOICES}
DECISIONS = [d.value for d in Decision]

# Fields scored, with how to derive the expected value from a spec.
FIELDS = [
    ("invoice_number", lambda s: s["invoice_number"]),
    ("invoice_date",   lambda s: "-".join(reversed(s["date"].split("/")))),
    ("vendor_gstin",   lambda s: None),   # compared against the vendor master
    ("po_reference",   lambda s: s["po_ref"]),
    ("subtotal_paise", lambda s: R(s["subtotal"])),
    ("gst_rate",       lambda s: s["gst_rate"]),
    ("tax_paise",      lambda s: R(s["subtotal"] * s["gst_rate"] / 100)),
    ("total_paise",    lambda s: R(s["subtotal"]) + R(s["subtotal"] * s["gst_rate"] / 100)),
]

# EC-4 deliberately destroys this field. A null is the CORRECT answer, so
# scoring it against the spec would penalise a correct refusal and reward a
# model that hallucinated.
NOT_SCORED = {"17_ILLEGIBLE": {"invoice_number"}}


def answer_key() -> dict[str, dict]:
    with open(config.ANSWER_KEY_PATH, newline="", encoding="utf-8") as f:
        return {r["invoice_file"].removesuffix(".pdf"): r for r in csv.DictReader(f)}


def corpus() -> list:
    files = list(config.CLEAN_INVOICE_DIR.glob("*.pdf")) + \
            list(config.SCANNED_INVOICE_DIR.glob("*.pdf"))
    return sorted(files, key=lambda p: p.stem)


def run_tier(tier: Tier) -> dict:
    store.load_masters_into_db(verbose=False)
    traces = {}
    for path in corpus():
        try:
            traces[path.stem] = process_invoice(path, tier, today=BATCH_DATE)
        except Exception as exc:
            traces[path.stem] = exc
    return traces


# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------

def score_extraction(traces: dict) -> tuple[dict, list, int]:
    """Per-field correctness. Gated invoices are excluded -- see the note."""
    tally = defaultdict(lambda: [0, 0])   # field -> [correct, total]
    misses, gated = [], 0

    for stem, trace in traces.items():
        if isinstance(trace, Exception):
            continue
        q = trace.stage_1_extraction.quality
        if q and not q.passes_gate:
            gated += 1
            continue
        spec = SPECS.get(stem)
        if not spec:
            continue

        fields = trace.stage_1_extraction.fields
        vendor_master = store.get_vendor(spec["vendor_id"])

        for name, expected_fn in FIELDS:
            if name in NOT_SCORED.get(stem, set()):
                continue
            expected = (vendor_master.gstin if name == "vendor_gstin"
                        else expected_fn(spec))
            got = getattr(fields, name).value
            tally[name][1] += 1
            if got == expected:
                tally[name][0] += 1
            else:
                misses.append((stem, name, got, expected))

    return dict(tally), misses, gated


def score_matching(traces: dict) -> tuple[Counter, list, Counter]:
    correct, misses, by_layer = Counter(), [], Counter()
    for stem, trace in traces.items():
        if isinstance(trace, Exception):
            continue
        m = trace.stage_2_matching
        spec = SPECS.get(stem)
        if not spec:
            continue
        q = trace.stage_1_extraction.quality
        if q and not q.passes_gate:
            correct["gated"] += 1
            continue
        by_layer[m.match_layer or 0] += 1
        if m.po_number == spec["po_ref"]:
            correct["right"] += 1
        else:
            correct["wrong"] += 1
            misses.append((stem, m.po_number, spec["po_ref"], m.match_status.value))
    return correct, misses, by_layer


def score_decisions(traces: dict, key: dict, tier: Tier):
    col = "expected_decision" if tier is Tier.PREMIUM else "expected_decision_free"
    rule_col = ("expected_determining_rules" if tier is Tier.PREMIUM
                else "expected_determining_rules_free")

    matrix = defaultdict(Counter)     # expected -> actual
    wrong_decision, wrong_reason = [], []

    for stem, trace in traces.items():
        if isinstance(trace, Exception):
            continue
        d = trace.stage_5_decision
        got, expected = d.decision.value, key[stem][col]
        matrix[expected][got] += 1

        if got != expected:
            wrong_decision.append((stem, got, expected))
            continue

        want = {r for r in key[stem][rule_col].split("|") if r}
        have = set(d.determined_by)
        if want != have:
            wrong_reason.append((stem, sorted(have), sorted(want)))

    return matrix, wrong_decision, wrong_reason


# ---------------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------------

def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def report_tier(out: list, tier: Tier, traces: dict, key: dict) -> dict:
    total = len(traces)
    errored = [s for s, t in traces.items() if isinstance(t, Exception)]

    out.append(f"\n## {tier.value.title()} tier\n")
    if errored:
        out.append(f"**{len(errored)} invoice(s) could not be processed:**\n")
        for stem in errored:
            out.append(f"- `{stem}` — {traces[stem]}")
        out.append("")

    # --- 1. extraction ----------------------------------------------------
    tally, misses, gated = score_extraction(traces)
    correct = sum(v[0] for v in tally.values())
    checked = sum(v[1] for v in tally.values())

    out.append("### 1. Extraction accuracy\n")
    out.append(f"{correct}/{checked} field values correct ({pct(correct, checked)}). "
               f"{gated} invoice(s) excluded — the quality gate stopped them, so "
               f"nothing was claimed and there is nothing to score.\n")
    out.append("| Field | Correct | Checked | Accuracy |")
    out.append("|---|---|---|---|")
    for name, _ in FIELDS:
        c, t = tally.get(name, [0, 0])
        out.append(f"| `{name}` | {c} | {t} | {pct(c, t)} |")
    if misses:
        out.append("\n**Misses**\n")
        for stem, field, got, want in misses:
            out.append(f"- `{stem}` `{field}`: got `{got}`, expected `{want}`")
    out.append("")

    # --- 2. matching ------------------------------------------------------
    m_counts, m_misses, by_layer = score_matching(traces)
    right, wrong = m_counts["right"], m_counts["wrong"]
    out.append("### 2. Match accuracy\n")
    out.append(f"{right}/{right + wrong} matched to the correct purchase order "
               f"({pct(right, right + wrong)}). {m_counts['gated']} gated.\n")
    out.append("| Layer | How | Count |")
    out.append("|---|---|---|")
    names = {1: "explicit PO reference printed on the invoice",
             2: "inferred from vendor, amount and date",
             3: "ambiguous — several candidates returned",
             4: "no match", 0: "not attempted"}
    for layer in sorted(by_layer):
        out.append(f"| {layer or '—'} | {names[layer]} | {by_layer[layer]} |")
    for stem, got, want, status in m_misses:
        out.append(f"\n- `{stem}`: matched `{got}` ({status}), expected `{want}`")
    out.append("")

    # --- 3. decisions -----------------------------------------------------
    matrix, wrong_d, wrong_r = score_decisions(traces, key, tier)
    scored = total - len(errored)
    right_d = scored - len(wrong_d)

    out.append("### 3. Decision accuracy\n")
    out.append(f"**{right_d}/{scored} ({pct(right_d, scored)})**\n")
    out.append("Rows are expected, columns are actual.\n")
    header = "| expected \\ actual | " + " | ".join(
        d.replace("_", " ").title() for d in DECISIONS) + " |"
    out.append(header)
    out.append("|---" * (len(DECISIONS) + 1) + "|")
    for exp in DECISIONS:
        cells = []
        for act in DECISIONS:
            n = matrix[exp][act]
            cells.append(f"**{n}**" if exp == act and n else (str(n) if n else "·"))
        out.append(f"| {exp.replace('_', ' ').title()} | " + " | ".join(cells) + " |")
    for stem, got, want in wrong_d:
        out.append(f"\n- `{stem}`: decided **{got}**, expected **{want}**")
    out.append("")

    # --- 4. reasons -------------------------------------------------------
    right_r = right_d - len(wrong_r)
    out.append("### 4. Reason accuracy\n")
    out.append(f"**{right_r}/{scored} ({pct(right_r, scored)})** — of the "
               f"{right_d} correct decisions, {right_r} also cited exactly the "
               f"expected rule(s).\n")
    out.append("> A right decision for the wrong reason is not correct. It is "
               "lucky, and it will be wrong on the next invoice.\n")
    for stem, have, want in wrong_r:
        out.append(f"- `{stem}`: cited `{', '.join(have) or 'none'}`, "
                   f"expected `{', '.join(want) or 'none'}`")
    out.append("")

    return {
        "extraction": (correct, checked), "match": (right, right + wrong),
        "decision": (right_d, scored), "reason": (right_r, scored),
        "gated": gated, "errored": len(errored),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["free", "premium", "both"], default="free")
    args = ap.parse_args()

    wanted = {"free": [Tier.FREE], "premium": [Tier.PREMIUM],
              "both": [Tier.FREE, Tier.PREMIUM]}[args.tier]

    if Tier.PREMIUM in wanted and not os.environ.get(config.OPENAI_API_KEY_ENV):
        try:
            from dotenv import load_dotenv
            load_dotenv(config.ROOT_DIR / ".env")
        except ImportError:
            pass
    skipped_premium = False
    if Tier.PREMIUM in wanted and not os.environ.get(config.OPENAI_API_KEY_ENV):
        wanted = [t for t in wanted if t is not Tier.PREMIUM]
        skipped_premium = True
        if not wanted:
            raise SystemExit(f"{config.OPENAI_API_KEY_ENV} is not set.")

    key = answer_key()
    out: list[str] = []
    out.append("# Validation scorecard\n")
    out.append(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  ")
    out.append(f"Corpus: {len(corpus())} invoices  ")
    out.append(f"Rules: 33  ")
    out.append(f"Batch date fixed at {BATCH_DATE} so future-date checks are "
               f"reproducible\n")

    summaries = {}
    for tier in wanted:
        summaries[tier] = report_tier(out, tier, run_tier(tier), key)

    out.append("## Summary\n")
    out.append("| Metric | " + " | ".join(t.value for t in wanted) + " |")
    out.append("|---" * (len(wanted) + 1) + "|")
    for label, k in [("Extraction", "extraction"), ("Match", "match"),
                     ("Decision", "decision"), ("Reason", "reason")]:
        cells = []
        for t in wanted:
            c, d = summaries[t][k]
            cells.append(f"{c}/{d} ({pct(c, d)})")
        out.append(f"| {label} | " + " | ".join(cells) + " |")

    out.append("\n## How to read these numbers\n")
    out.append("High scores here are less impressive than they look, and it is "
               "worth being straight about why.\n")
    out.append("**Extraction is exact by construction on the free tier.** It "
               "reads the text layer a born-digital PDF already contains -- "
               "the literal characters, not a guess at pixels. Anything other "
               "than 100% would mean a label-matching bug, not a recognition "
               "failure. The premium tier is the one where this metric earns "
               "its keep.\n")
    out.append("**The corpus is synthetic.** These invoices were generated for "
               "this project, so they cannot surprise it. Four visually "
               "distinct templates with different label wording and a "
               "CGST/SGST split give real variety, but they are still variety "
               "that was anticipated. A corpus of genuine vendor invoices "
               "would be a far harder test.\n")
    out.append("**Decision and reason accuracy are the meaningful figures.** "
               "The answer key was written in Phase 3, before any extraction, "
               "matching or rule code existed. Those expectations were not "
               "adjusted to fit the engine -- with one documented exception: "
               "three rows were updated when R-303 was implemented as a "
               "cross-check of R-302 and correctly began failing alongside "
               "it.\n")
    out.append("**What would genuinely test this:** real invoices from real "
               "vendors, a PO master someone else built, and an answer key "
               "written by someone who had not seen the rules.\n")
    out.append("## What is not measured\n")
    out.append("- **Layer 3 matching (ambiguous).** No two POs in the master "
               "fall inside the ±10% amount window together, so the path is "
               "covered by unit test rather than by corpus.")
    out.append("- **EC-4's invoice number.** Excluded from extraction scoring: "
               "the scan destroyed it, so a null is the correct answer and "
               "scoring it would reward a hallucination.")
    out.append("- **Gated invoices.** Excluded from extraction and match "
               "scoring. Nothing was claimed, so there is nothing to be right "
               "or wrong about. The gate outcome is itself scored under "
               "decisions.")
    if skipped_premium:
        out.append("- **The premium tier.** Not run: "
                   f"`{config.OPENAI_API_KEY_ENV}` was not set.")
    out.append("")

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.SCORECARD_PATH.write_text("\n".join(out), encoding="utf-8")

    print("\n".join(out[-14:]))
    print(f"\nWritten to {config.SCORECARD_PATH}")


if __name__ == "__main__":
    main()
