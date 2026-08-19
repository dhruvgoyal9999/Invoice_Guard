"""
Walk through the four edge cases with commentary.

    python -m scripts.demo_edge_cases              free tier
    python -m scripts.demo_edge_cases --tier premium
    python -m scripts.demo_edge_cases --only 2     just EC-2

Written to be read aloud. Each case states the scenario, why it is hard, what
the system did, and what that demonstrates.

The recurring point: NONE of these required special-case code. Every one falls
out of a general rule. If any of them needed an `if` on a specific invoice, it
would have stopped being a demonstration of judgement and become a hard-coded
exception.
"""

import argparse
from datetime import date

from src import config, store
from src.money import format_paise
from src.pipeline import process_invoice
from src.schemas import RuleStatus, Tier

BATCH_DATE = date(2026, 8, 10)
W = 78


def hr(char="-"):
    print(char * W)


def header(title: str, subtitle: str = ""):
    print()
    hr("=")
    print(f"  {title}")
    if subtitle:
        print(f"  {subtitle}")
    hr("=")


def block(label: str, body: str):
    print(f"\n{label}")
    for line in body.strip().splitlines():
        print(f"  {line.strip()}")


def run(stem: str, tier: Tier, folder=None):
    folder = folder or config.CLEAN_INVOICE_DIR
    return process_invoice(folder / f"{stem}.pdf", tier, today=BATCH_DATE)


def rule_of(trace, rule_id):
    return next((r for r in trace.stage_4_rules if r.rule_id == rule_id), None)


def outcome_line(stem, trace):
    d = trace.stage_5_decision
    by = ", ".join(d.determined_by) or "no rule failed"
    return f"{stem:<20} {d.decision.value:<19} {by}"


# ---------------------------------------------------------------------------

def ec1(tier: Tier):
    header("EC-1  PROGRESSIVE BILLING",
           "One PO, several invoices, and an overspend no single invoice reveals")

    block("SCENARIO", """
        PO-1010 is a Rs 10,00,000 consulting engagement delivered in phases.
        Meridian bills three times: Rs 4,00,000, Rs 3,50,000, Rs 2,60,000.
        Each invoice on its own is far below the PO value.
    """)

    block("WHY IT IS HARD", """
        A system comparing each invoice against the FULL PO total approves all
        three and pays Rs 10,10,000 against a Rs 10,00,000 commitment. The
        overspend is invisible at the level of any single invoice -- every one
        of them looks like a modest under-delivery.
    """)

    print("\nSEQUENCE")
    hr()
    for stem in ["10_INV-MER-3312", "11_INV-MER-3348",
                 "12_INV-MER-3390", "13_INV-MER-3391"]:
        t = run(stem, tier)
        f = t.stage_3_financials
        print(f"  {outcome_line(stem, t)}")
        if f:
            print(f"      remaining before {format_paise(f.remaining_balance_paise):>16}"
                  f"   overage {format_paise(f.overage_paise):>16}")

    block("THE BOUNDARY", """
        The third invoice lands on an overage of exactly Rs 10,000 against an
        allowance of exactly Rs 10,000. It PASSES, because the rule uses
        'greater than', not 'greater than or equal'. One paise more and it
        would have been held.

        The fourth arrives against a PO whose remaining balance is now
        negative. It is held.
    """)

    block("WHAT IT DEMONSTRATES", """
        State awareness -- the system treats a PO as a running balance, not a
        static number.

        And it required NO SPECIAL CODE. Tolerance is measured against the
        REMAINING BALANCE rather than the PO total, so progressive billing
        falls out of the ordinary path. That single design choice is what makes
        this work.
    """)


def ec2(tier: Tier):
    header("EC-2  RECURRING BILLING vs A GENUINE DUPLICATE",
           "Three invoices that are identical on every obvious signal")

    block("SCENARIO", """
        Sharma Logistics has a monthly freight retainer on PO-1011. Three
        invoices arrive, each for exactly Rs 45,000, days apart, with different
        invoice numbers. Two are legitimate. One is a duplicate.
    """)

    block("WHY IT IS HARD", """
        Same vendor. Same amount. Same PO. Within days of each other. Every
        naive duplicate heuristic fires on all three.

        Auto-rejecting means refusing to pay a vendor for work they did.
        Auto-approving means a real duplicate sails through the same gap.
        The amount and the date tell you nothing.
    """)

    print("\nSEQUENCE")
    hr()
    for stem in ["14_SL-9012", "15_SL-9034", "16_SL-9047"]:
        t = run(stem, tier)
        sp = t.stage_1_extraction.fields.service_period.value
        period = f"{sp.from_date} to {sp.to_date}" if sp else "no period"
        print(f"  {outcome_line(stem, t)}")
        print(f"      service period: {period}")
        r = rule_of(t, "R-502")
        if r:
            print(f"      R-502 {r.status.value}: {r.message[:100]}")

    block("THE DISTINGUISHING SIGNAL", """
        Service period. Nothing else separates these three.

        Periods present and non-overlapping -> legitimate recurring billing
        Periods present and overlapping     -> genuine duplicate suspicion
        Either period missing               -> cannot determine, so hold
    """)

    block("WHAT IT DEMONSTRATES", """
        That the extractor goes looking for a field a naive implementation
        would never think to extract -- because the rule was reasoned about
        first, and the extraction requirement followed from it.

        Note also that the first invoice is checked against SL-8856 from the
        seeded history, an invoice it never met, and correctly cleared.
    """)


def ec3(tier: Tier):
    header("EC-3  WHICH TOLERANCE LIMIT BINDS",
           "min(1.5% of PO, Rs 10,000) -- and the two ways it can fail")

    block("SCENARIO", """
        Two invoices, both over their PO, both held. But for opposite reasons.

        PO-1005 is Rs 20,00,000. The invoice is Rs 20,25,000 -- an overage of
        Rs 25,000, which is 1.25% of the PO. That is UNDER the 1.5% threshold.

        PO-1003 is Rs 1,25,000. The invoice is Rs 1,30,000 -- an overage of
        Rs 5,000, which is UNDER the Rs 10,000 cap.
    """)

    block("WHY IT IS HARD", """
        Each invoice passes one of the two limits. A single-threshold
        implementation approves one of them, and which one depends on which
        threshold the implementer happened to pick.
    """)

    print("\nRESULTS")
    hr()
    for stem in ["08_INV-NEX-5612", "09_SL-8834"]:
        t = run(stem, tier)
        f = t.stage_3_financials
        print(f"  {outcome_line(stem, t)}")
        if f:
            print(f"      overage {format_paise(f.overage_paise):>14}"
                  f"   allowed {format_paise(f.allowed_overage_paise):>14}"
                  f"   bound by {f.binding_constraint}")
            print(f"      1.5% would allow {format_paise(f.percent_allowance_paise)};"
                  f" cap is {format_paise(f.cap_paise)}")
        r = rule_of(t, "R-302")
        if r:
            print(f"      R-302: {r.message[:110]}")

    block("THE CROSSOVER", f"""
        The two limits are equal at a PO of {format_paise(config.TOLERANCE_CROSSOVER_PAISE)}.
        Below that the percentage binds; above it, the cap.

        PO-1008 (Rs 7,00,000) and PO-1009 (Rs 6,66,666) sit either side of it.
        PO-1009's allowance is Rs 9,999.99 -- one paise under the cap.
    """)

    block("WHAT IT DEMONSTRATES", """
        That min() of two limits is genuinely implemented, and that the trace
        says WHICH limit bound and what the other would have permitted. That
        sentence is the difference between a system that has a rule and one
        that can explain its rule.
    """)


def ec4(tier: Tier):
    header("EC-4  A FIELD THAT CANNOT BE READ",
           "Graceful degradation, and why SKIP is not PASS")

    block("SCENARIO", """
        A scanned invoice, rotated and noisy, rendered at 82 dpi. The vendor,
        the PO reference, the line items and the totals are all legible. The
        invoice number is not -- it was printed faintly and the scan destroyed
        it.
    """)

    block("WHY IT IS HARD", """
        This is where a language model is most likely to be helpfully wrong:
        inventing something that looks like an invoice number.

        A fabricated invoice number is worse than a missing one. It silently
        defeats duplicate detection forever, because no future invoice will
        ever match it.
    """)

    t = run("17_ILLEGIBLE", tier, config.SCANNED_INVOICE_DIR)
    print("\nRESULT")
    hr()
    print(f"  {outcome_line('17_ILLEGIBLE', t)}")
    skipped = [r for r in t.stage_4_rules if r.status == RuleStatus.SKIP]
    failed = [r for r in t.stage_4_rules if r.status == RuleStatus.FAIL]
    print(f"      {len(failed)} failed, {len(skipped)} could not be run")
    for r in failed:
        print(f"      {r.rule_id} FAIL: {r.message[:104]}")
    r501 = rule_of(t, "R-501")
    if r501:
        print(f"      R-501 {r501.status.value}: {r501.message[:104]}")

    if tier is Tier.FREE:
        block("NOTE ON TIERS", """
            On the FREE tier this is stopped earlier, by the quality gate
            (R-010) -- the extractor reads embedded text and this document has
            none. Every other rule reports SKIP.

            On the PREMIUM tier the document IS read, the invoice number comes
            back null with a reason, and R-001 does the holding instead. Same
            decision, different reason -- which is exactly why the answer key
            checks the determining rule, not just the outcome.
        """)

    block("THE POINT", """
        R-501, the duplicate check, returns SKIP -- never PASS.

        A check that could not run must never read as a check that succeeded.
        Without a distinct SKIP status the system would have to choose between
        falsely passing and falsely failing, and both are lies in an audit
        trail.
    """)

    block("WHAT IT DEMONSTRATES", """
        Honest handling of uncertainty. The system fails usefully: partial
        results, a specific reason, and a named next step -- rather than
        crashing, guessing, or quietly approving.
    """)


# ---------------------------------------------------------------------------

CASES = {1: ec1, 2: ec2, 3: ec3, 4: ec4}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["free", "premium"], default="free")
    ap.add_argument("--only", type=int, choices=[1, 2, 3, 4])
    args = ap.parse_args()
    tier = Tier.FREE if args.tier == "free" else Tier.PREMIUM

    store.load_masters_into_db(verbose=False)
    print(f"\nDatabase rebuilt. Running on the {tier.value} tier.")

    for n in ([args.only] if args.only else [1, 2, 3, 4]):
        CASES[n](tier)

    print()
    hr("=")
    print("  None of these required special-case code.")
    print("  Every one falls out of a general rule.")
    hr("=")
    print()


if __name__ == "__main__":
    main()
