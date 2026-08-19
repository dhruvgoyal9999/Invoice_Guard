"""
Check the test corpus is internally consistent before any extraction is built.

    python -m scripts.verify_corpus

Catches the mistakes that are cheap to fix now and expensive to diagnose in
Phase 10: a renamed file, an answer-key row pointing at nothing, an invoice
sitting on disk that nobody expects, a PO reference that does not exist.

Exits non-zero if anything fails, so it can gate a build later.
"""

import csv
import sys
from collections import Counter

from src import config, store
from scripts.invoice_specs import INVOICES, VENDORS

VALID_DECISIONS = {
    "AUTO_APPROVE", "APPROVE_WITH_FLAG", "HOLD_FOR_REVIEW", "REJECT",
}

problems: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


def read_answer_key() -> list[dict]:
    with open(config.ANSWER_KEY_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def locate(filename: str):
    """Find an invoice in either clean/ or scanned/. Returns (path, folder)."""
    for folder, d in [("clean", config.CLEAN_INVOICE_DIR),
                      ("scanned", config.SCANNED_INVOICE_DIR)]:
        p = d / filename
        if p.exists():
            return p, folder
    return None, None


def main() -> None:
    store.load_masters_into_db(verbose=False)
    key = read_answer_key()

    print(f"Answer key rows      {len(key)}")
    print(f"Invoice specs        {len(INVOICES)}")
    clean = sorted(p.name for p in config.CLEAN_INVOICE_DIR.glob("*.pdf"))
    scanned = sorted(p.name for p in config.SCANNED_INVOICE_DIR.glob("*.pdf"))
    print(f"Files on disk        {len(clean)} clean + {len(scanned)} scanned "
          f"= {len(clean) + len(scanned)}")
    print()

    # --- 1. counts agree ---------------------------------------------------
    if len(key) != len(INVOICES):
        fail(f"answer_key has {len(key)} rows but invoice_specs has {len(INVOICES)}")

    # --- 2. every answer-key row has a file, in the right folder -----------
    seen = set()
    for row in key:
        fn = row["invoice_file"]
        seen.add(fn)
        path, folder = locate(fn)
        if path is None:
            fail(f"{fn}: listed in answer_key but not found on disk")
            continue
        expected_folder = "scanned" if row["source_type"] == "scanned_image" else "clean"
        if folder != expected_folder:
            fail(f"{fn}: answer_key says {row['source_type']} "
                 f"but file is in {folder}/")

    # --- 3. no stray files -------------------------------------------------
    for fn in clean + scanned:
        if fn not in seen:
            fail(f"{fn}: on disk but absent from answer_key")

    # --- 4. decisions and rule ids are well formed -------------------------
    for row in key:
        d = row["expected_decision"]
        if d not in VALID_DECISIONS:
            fail(f"{row['invoice_file']}: unknown decision '{d}'")
        rules = row["expected_determining_rules"].strip()
        if rules:
            for r in rules.split("|"):
                if not r.startswith("R-") or len(r) != 5:
                    fail(f"{row['invoice_file']}: malformed rule id '{r}'")
        elif d != "AUTO_APPROVE":
            fail(f"{row['invoice_file']}: {d} must name a determining rule")

    # --- 5. answer key agrees with invoice_specs ---------------------------
    by_stem = {s["stem"]: s for s in INVOICES}
    for row in key:
        stem = row["invoice_file"].removesuffix(".pdf")
        spec = by_stem.get(stem)
        if spec is None:
            fail(f"{stem}: in answer_key but not in invoice_specs")
            continue
        if spec["vendor_id"] != row["vendor_id"]:
            fail(f"{stem}: vendor mismatch "
                 f"({spec['vendor_id']} vs {row['vendor_id']})")
        if spec["po_ref"] != row["expected_po"]:
            fail(f"{stem}: PO mismatch "
                 f"({spec['po_ref']} vs {row['expected_po']})")
        if float(row["expected_subtotal_rupees"]) != spec["subtotal"]:
            fail(f"{stem}: subtotal mismatch "
                 f"({spec['subtotal']} vs {row['expected_subtotal_rupees']})")
        want_scan = spec.get("scan", False)
        is_scan = row["source_type"] == "scanned_image"
        if want_scan != is_scan:
            fail(f"{stem}: scan flag disagrees with answer_key source_type")

    # --- 6. every referenced PO and vendor exists --------------------------
    for spec in INVOICES:
        if spec["vendor_id"] not in VENDORS:
            fail(f"{spec['stem']}: unknown vendor {spec['vendor_id']}")
        if store.get_po(spec["po_ref"]) is None:
            fail(f"{spec['stem']}: PO {spec['po_ref']} not in po_master")

    # --- 7. line items reconcile ------------------------------------------
    for spec in INVOICES:
        calc = sum(q * r for _, q, r in spec["lines"])
        if calc != spec["subtotal"]:
            fail(f"{spec['stem']}: lines sum to {calc:,} "
                 f"but subtotal is {spec['subtotal']:,}")

    # --- 8. vendors with no seeded history --------------------------------
    for vid in sorted(VENDORS):
        if store.count_prior_invoices(vid) == 0:
            notes.append(f"{vid} has no invoice history -- R-203 will fire")

    # --- 9. batch order is unambiguous ------------------------------------
    stems = [s["stem"] for s in INVOICES]
    if stems != sorted(stems):
        fail("invoice stems do not sort into processing order")
    seqs = [s["seq"] for s in INVOICES]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        fail("seq numbers are not unique and ascending")

    # --- report -----------------------------------------------------------
    dist = Counter(r["expected_decision"] for r in key)
    print("Expected decisions")
    for d in ["AUTO_APPROVE", "APPROVE_WITH_FLAG", "HOLD_FOR_REVIEW", "REJECT"]:
        print(f"  {d:<20} {dist.get(d, 0)}")
    print()

    tpl = Counter(VENDORS[s["vendor_id"]]["template"] for s in INVOICES)
    print("Templates in use")
    for t, n in sorted(tpl.items()):
        print(f"  {t:<14} {n}")
    print()

    if notes:
        print("Notes")
        for n in notes:
            print(f"  - {n}")
        print()

    if problems:
        print(f"FAILED -- {len(problems)} problem(s)")
        for p in problems:
            print(f"  x {p}")
        sys.exit(1)

    print("Corpus verified. All checks passed.")


if __name__ == "__main__":
    main()
