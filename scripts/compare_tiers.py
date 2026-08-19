"""
Run both extraction tiers over the corpus and show where they diverge.

    python -m scripts.compare_tiers                 both tiers
    python -m scripts.compare_tiers --tier free     no API key needed
    python -m scripts.compare_tiers --tier premium
    python -m scripts.compare_tiers --no-cache      force fresh extraction

Scores every field against the ground truth in invoice_specs.py, reports the
quality gate outcome per tier, and lists the invoices where the two tiers
disagree. That divergence list is the point of the whole two-tier design:
same rules, same PO data, different extraction quality.

Premium is skipped with a clear message if OPENAI_API_KEY is not set, so this
runs end to end on a machine with no key at all.
"""

import argparse
import os
from collections import Counter

from src import config
from src.extract import extract_invoice
from src.money import rupees_to_paise as R, format_paise
from src.schemas import Tier
from scripts.invoice_specs import INVOICES

SPECS = {s["stem"]: s for s in INVOICES}

# Fields we score, and how to pull the expected value out of a spec.
CHECKS = [
    ("invoice_number", lambda s: s["invoice_number"]),
    ("invoice_date",   lambda s: "-".join(reversed(s["date"].split("/")))),
    ("po_reference",   lambda s: s["po_ref"]),
    ("subtotal_paise", lambda s: R(s["subtotal"])),
    ("gst_rate",       lambda s: s["gst_rate"]),
    ("tax_paise",      lambda s: R(s["subtotal"] * s["gst_rate"] / 100)),
    ("total_paise",    lambda s: R(s["subtotal"]) + R(s["subtotal"] * s["gst_rate"] / 100)),
]

# EC-4: the invoice number was deliberately destroyed by the scan. A null here
# is the CORRECT answer, so scoring it against the spec would be misleading.
UNREADABLE = {"17_ILLEGIBLE": {"invoice_number"}}


def all_invoices():
    files = sorted(config.CLEAN_INVOICE_DIR.glob("*.pdf")) + \
            sorted(config.SCANNED_INVOICE_DIR.glob("*.pdf"))
    return sorted(files, key=lambda p: p.stem)


def run_tier(tier: Tier, use_cache: bool) -> dict:
    results = {}
    for path in all_invoices():
        try:
            results[path.stem] = extract_invoice(path, tier, use_cache=use_cache)
        except Exception as exc:  # a tier failing is data, not a crash
            results[path.stem] = exc
    return results


def score(results: dict) -> tuple[Counter, Counter, list[str]]:
    correct, total, misses = Counter(), Counter(), []
    for stem, stage in results.items():
        spec = SPECS.get(stem)
        if spec is None or isinstance(stage, Exception):
            continue
        if not stage.quality.passes_gate:
            continue  # gated out; nothing was claimed, so nothing to score
        for field, expected_fn in CHECKS:
            if field in UNREADABLE.get(stem, set()):
                continue
            total[field] += 1
            got = getattr(stage.fields, field).value
            if got == expected_fn(spec):
                correct[field] += 1
            else:
                misses.append(f"{stem}: {field} = {got!r}, expected {expected_fn(spec)!r}")
    return correct, total, misses


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["free", "premium", "both"], default="both")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    use_cache = not args.no_cache
    want = {"free": [Tier.FREE], "premium": [Tier.PREMIUM],
            "both": [Tier.FREE, Tier.PREMIUM]}[args.tier]

    if Tier.PREMIUM in want and not os.environ.get(config.OPENAI_API_KEY_ENV):
        try:
            from dotenv import load_dotenv
            load_dotenv(config.ROOT_DIR / ".env")
        except ImportError:
            pass
    if Tier.PREMIUM in want and not os.environ.get(config.OPENAI_API_KEY_ENV):
        print(f"! {config.OPENAI_API_KEY_ENV} not set -- skipping the premium tier.\n")
        want = [t for t in want if t is not Tier.PREMIUM]
        if not want:
            raise SystemExit("Nothing to run.")

    runs = {t: run_tier(t, use_cache) for t in want}

    # ---- quality gate ----------------------------------------------------
    print("QUALITY GATE")
    for tier, results in runs.items():
        ok = sum(1 for s in results.values()
                 if not isinstance(s, Exception) and s.quality.passes_gate)
        err = sum(1 for s in results.values() if isinstance(s, Exception))
        print(f"  {tier.value:<9} {ok} passed, {len(results) - ok - err} gated, "
              f"{err} errored")
    print()

    # ---- field accuracy --------------------------------------------------
    print("FIELD ACCURACY (against invoice_specs, gated invoices excluded)")
    scored = {t: score(r) for t, r in runs.items()}
    header = "  " + f"{'field':<17}" + "".join(f"{t.value:>12}" for t in runs)
    print(header)
    print("  " + "-" * (17 + 12 * len(runs)))
    for field, _ in CHECKS:
        row = f"  {field:<17}"
        for t in runs:
            c, tot, _ = scored[t]
            row += f"{c[field]}/{tot[field]:<8}".rjust(12) if tot[field] else "-".rjust(12)
        print(row)
    print()

    for t in runs:
        _, _, misses = scored[t]
        if misses:
            print(f"  {t.value} misses:")
            for m in misses:
                print(f"    x {m}")
            print()

    # ---- divergence ------------------------------------------------------
    if len(runs) == 2:
        print("DIVERGENCE")
        free, prem = runs[Tier.FREE], runs[Tier.PREMIUM]
        found = False
        for stem in sorted(free):
            f, p = free[stem], prem[stem]
            if isinstance(f, Exception) or isinstance(p, Exception):
                continue
            if f.quality.passes_gate != p.quality.passes_gate:
                found = True
                print(f"  {stem:<22} free: {'pass' if f.quality.passes_gate else 'GATED'}"
                      f"   premium: {'pass' if p.quality.passes_gate else 'GATED'}")
                reason = (f if not f.quality.passes_gate else p).quality.gate_reason
                print(f"    {reason[:88]}")
        if not found:
            print("  Tiers agree on every invoice.")
        print()

    # ---- per-invoice detail ---------------------------------------------
    print("PER INVOICE")
    print(f"  {'invoice':<22}{'tier':<9}{'gate':<7}{'inv#':<15}{'subtotal':>15}{'conf':>7}")
    print("  " + "-" * 75)
    for stem in sorted(runs[want[0]]):
        for t in runs:
            st = runs[t][stem]
            if isinstance(st, Exception):
                print(f"  {stem:<22}{t.value:<9}ERROR  {str(st)[:40]}")
                continue
            gate = "pass" if st.quality.passes_gate else "GATED"
            num = str(st.fields.invoice_number.value or "-")
            sub = format_paise(st.fields.subtotal_paise.value) \
                if st.fields.subtotal_paise.value is not None else "-"
            conf = f"{st.quality.mean_critical_confidence:.2f}"
            print(f"  {stem:<22}{t.value:<9}{gate:<7}{num:<15}{sub:>15}{conf:>7}")
    print()


if __name__ == "__main__":
    main()
