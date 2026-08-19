"""
Severity resolution. Spec Section 11.

    any BLOCKER  FAIL -> REJECT
    any CRITICAL FAIL -> HOLD_FOR_REVIEW
    any WARNING  FAIL -> APPROVE_WITH_FLAG
    else              -> AUTO_APPROVE

That is the entire decision engine. All the judgement lives in rules.py, where
each rule declares its own severity; this module only sorts. If logic starts
accumulating here, it belongs back in a rule.

Note what is NOT here: no amount thresholds, no vendor checks, no special
cases. A reader can verify the decision policy in one glance, which is exactly
what an auditor needs.
"""

from .schemas import Decision, DecisionResult, RuleResult, RuleStatus, Severity

# Checked in order. First severity with a failure wins.
_LADDER: list[tuple[Severity, Decision]] = [
    (Severity.BLOCKER, Decision.REJECT),
    (Severity.CRITICAL, Decision.HOLD_FOR_REVIEW),
    (Severity.WARNING, Decision.APPROVE_WITH_FLAG),
]

# Decisions that release money and therefore consume PO budget.
_ACCEPTING = {Decision.AUTO_APPROVE, Decision.APPROVE_WITH_FLAG}


def decide(results: list[RuleResult]) -> DecisionResult:
    """Resolve a full set of rule results into one outcome."""
    counts = {
        "rules_run": len(results),
        "rules_passed": sum(1 for r in results if r.status == RuleStatus.PASS),
        "rules_failed": sum(1 for r in results if r.status == RuleStatus.FAIL),
        "rules_skipped": sum(1 for r in results if r.status == RuleStatus.SKIP),
    }

    failures = [r for r in results if r.status == RuleStatus.FAIL]

    for severity, decision in _LADDER:
        determining = [r.rule_id for r in failures if r.severity == severity]
        if determining:
            return DecisionResult(
                decision=decision, determined_by=sorted(determining), **counts
            )

    # INFO-severity failures deliberately reach here without changing anything.
    return DecisionResult(decision=Decision.AUTO_APPROVE, determined_by=[], **counts)


def is_accepted(decision: Decision) -> bool:
    """
    Whether this decision releases payment, and so consumes PO budget.

    Only accepted invoices may call store.update_already_invoiced(). A held
    invoice must not eat budget it was never approved for -- otherwise a
    queue of pending reviews would silently exhaust a PO.
    """
    return decision in _ACCEPTING
