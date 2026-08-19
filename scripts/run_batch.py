"""
Process the whole corpus and score it against the answer key.

    python -m scripts.run_batch                    free tier
    python -m scripts.run_batch --tier premium     needs an API key
    python -m scripts.run_batch --no-commit        assess without side effects

Rebuilds the database first so every run starts from the same state --
`already_invoiced` is mutable, so without a reset the second run would
double-count and every result after the first would be wrong.

Order matters: invoices are processed in filename order, which is why they are
numbered. The EC-1 progressive-billing sequence only lands on its boundary if
its three tranches are processed in order.
"""

import argparse
import csv
from collections import Counter
from datetime import date

from src import config, store
from src.pipeline import process_and_save
from src.schemas import RuleStatus, Tier
from src.trace import write_trace

BATCH_DATE = date(2026, 8, 10)  # fixed so future-date checks are reproducible


def answer_key() -> dict[str, dict]:
    with open(config.ANSWER_KEY_PATH, newline="", encoding="utf-8") as f:
        return {r["invoice_file"].removesuffix(".pdf"): r for r in csv.DictReader(f)}


def corpus() -> list:
    files = list(config.CLEAN_INVOICE_DIR.glob("*.pdf")) + \
            list(config.SCANNED_INVOICE_DIR.glob("*.pdf"))
    return sorted(files, key=lambda p: p.stem)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["free", "premium"], default="free")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    tier = Tier.FREE if args.tier == "free" else Tier.PREMIUM
    key = answer_key()
    dec_col = "expected_decision" if tier is Tier.PREMIUM else "expected_decision_free"
    rule_col = ("expected_determining_rules" if tier is Tier.PREMIUM
                else "expected_determining_rules_free")

    store.load_masters_into_db(verbose=False)
    print(f"Database rebuilt. Processing {len(corpus())} invoices on the "
          f"{tier.value} tier.\n")

    print(f"{'invoice':<22}{'decision':<19}{'by':<16}{'expected':<19}{'':<4}rules")
    print("-" * 92)

    wrong_decision, wrong_rules = [], []
    counts = Counter()

    for path in corpus():
        trace, out = process_and_save(
            path, tier, use_cache=not args.no_cache,
            commit=not args.no_commit, today=BATCH_DATE,
        )
        d = trace.stage_5_decision
        got, by = d.decision.value, ",".join(d.determined_by) or "-"
        exp = key[path.stem][dec_col]
        exp_by = key[path.stem][rule_col].replace("|", ",") or "-"

        ok = got == exp
        rules_ok = set(d.determined_by) == set(
            r for r in key[path.stem][rule_col].split("|") if r
        )
        if not ok:
            wrong_decision.append((path.stem, got, exp))
        elif not rules_ok:
            wrong_rules.append((path.stem, by, exp_by))

        counts[got] += 1
        mark = "ok" if ok and rules_ok else ("~" if ok else "XX")
        print(f"{path.stem:<22}{got:<19}{by:<16}{exp:<19}{mark:<4}"
              f"{d.rules_passed}P/{d.rules_failed}F/{d.rules_skipped}S")

    print()
    print("Decision distribution")
    for k in ["AUTO_APPROVE", "APPROVE_WITH_FLAG", "HOLD_FOR_REVIEW", "REJECT"]:
        print(f"  {k:<20}{counts[k]}")

    print()
    total = len(corpus())
    print(f"Decision accuracy   {total - len(wrong_decision)}/{total}")
    print(f"Reason accuracy     {total - len(wrong_decision) - len(wrong_rules)}/{total}")

    for stem, got, exp in wrong_decision:
        print(f"  XX {stem}: decided {got}, expected {exp}")
    for stem, got, exp in wrong_rules:
        print(f"  ~  {stem}: right decision, determined by {got}, expected {exp}")

    print(f"\nTraces written to {config.TRACE_DIR}")


if __name__ == "__main__":
    main()
