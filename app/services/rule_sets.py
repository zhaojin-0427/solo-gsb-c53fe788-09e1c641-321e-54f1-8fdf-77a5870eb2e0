"""Traceable rule-set domain logic.

Rule sets group threshold rules per jurisdiction. Draft submissions carry an
optimistic ``base_version`` and store every threshold **normalized to ppm**.

Overlap definition: two rules overlap when they share the same jurisdiction
(implicit — all rules of a set share it), allergen, ``applies_to`` and an equal
ppm threshold (boundaries included). ``declare`` and ``warning`` may coexist;
``prohibit`` conflicts with every other action, and equal-threshold duplicates
of the same action also conflict. Publishing a set with any conflict is
rejected with a deterministically ordered conflict report.
"""

from __future__ import annotations

import hashlib
import json
from itertools import combinations

from sqlalchemy.orm import Session

from ..errors import api_error
from ..models import RuleSet, RuleSetVersion
from ..schemas import RuleSetEntry
from ..units import concentration_to_ppm

#: declare + warning is the only overlapping action pair allowed to coexist.
COMPATIBLE_PAIR = frozenset({"declare", "warning"})

ACTIONS = ("declare", "warning", "prohibit")
APPLIES_TO = ("contains", "cross_contact")


def _r6(x: float) -> float:
    return round(x, 6)


def normalize_entries(entries: list[RuleSetEntry]) -> list[dict]:
    """Convert a submission to canonical, ppm-normalized rule dicts.

    Rules without an explicit code get a deterministic positional code
    (``auto-001`` …) that never collides with explicit codes.
    """
    taken = {e.code for e in entries if e.code is not None}
    rules: list[dict] = []
    for i, entry in enumerate(entries, start=1):
        code = entry.code
        if code is None:
            code = f"auto-{i:03d}"
            suffix = 1
            while code in taken:
                code = f"auto-{i:03d}-{suffix}"
                suffix += 1
            taken.add(code)
        rules.append(
            {
                "code": code,
                "allergen": entry.allergen,
                "threshold_ppm": _r6(concentration_to_ppm(entry.threshold, entry.unit)),
                "action": entry.action,
                "applies_to": entry.applies_to,
            }
        )
    return rules


def _conflict_reason(a: dict, b: dict) -> str | None:
    if a["action"] == "prohibit" or b["action"] == "prohibit":
        return "prohibit_conflict"
    if a["action"] == b["action"]:
        return "duplicate_action"
    # The only remaining combination is declare + warning, which is allowed.
    if frozenset((a["action"], b["action"])) == COMPATIBLE_PAIR:
        return None
    return "action_conflict"  # pragma: no cover - all pairs handled above


def find_conflicts(rules: list[dict]) -> list[dict]:
    """Return stably ordered conflict details for overlapping rules."""
    groups: dict[tuple, list[dict]] = {}
    for rule in rules:
        key = (rule["allergen"], rule["applies_to"], rule["threshold_ppm"])
        groups.setdefault(key, []).append(rule)

    conflicts: list[dict] = []
    for (allergen, applies_to, threshold_ppm), members in groups.items():
        for a, b in combinations(members, 2):
            # deterministic left/right ordering inside each pair
            left, right = sorted((a, b), key=lambda r: r["code"])
            reason = _conflict_reason(left, right)
            if reason is None:
                continue
            conflicts.append(
                {
                    "allergen": allergen,
                    "applies_to": applies_to,
                    "threshold_ppm": threshold_ppm,
                    "rule_a": {"code": left["code"], "action": left["action"]},
                    "rule_b": {"code": right["code"], "action": right["action"]},
                    "reason": reason,
                }
            )
    conflicts.sort(
        key=lambda c: (
            c["allergen"],
            c["applies_to"],
            c["threshold_ppm"],
            c["rule_a"]["code"],
            c["rule_b"]["code"],
        )
    )
    return conflicts


def rules_digest(rules: list[dict]) -> str:
    """Stable content hash of a normalized rule set."""
    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def summary_of(rules: list[dict]) -> dict:
    """Build the traceability summary stored with every published snapshot."""
    thresholds = [r["threshold_ppm"] for r in rules]
    return {
        "rule_count": len(rules),
        "allergens": sorted({r["allergen"] for r in rules}),
        "actions": {a: sum(1 for r in rules if r["action"] == a) for a in ACTIONS},
        "applies_to": {
            t: sum(1 for r in rules if r["applies_to"] == t) for t in APPLIES_TO
        },
        "threshold_ppm": {
            "min": _r6(min(thresholds)) if thresholds else None,
            "max": _r6(max(thresholds)) if thresholds else None,
        },
        "rule_codes": sorted(r["code"] for r in rules),
        "digest": rules_digest(rules),
    }


def resolve_binding(
    db: Session, code: str, version: int, jurisdiction: str | None = None
) -> tuple[RuleSet, RuleSetVersion]:
    """Resolve an evaluation binding to a published, immutable rule-set snapshot.

    Raises stable API errors for a missing rule set / version or an unpublished
    version; optionally verifies the jurisdiction matches.
    """
    rule_set = db.query(RuleSet).filter(RuleSet.code == code).first()
    if rule_set is None:
        raise api_error(404, "RULE_SET_NOT_FOUND", f"unknown rule set: {code!r}")
    snapshot = (
        db.query(RuleSetVersion)
        .filter(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.version == version)
        .first()
    )
    if snapshot is None:
        if version <= rule_set.current_version:
            raise api_error(
                409,
                "RULE_SET_NOT_PUBLISHED",
                f"rule set {code!r} version {version} is not published",
                current_version=rule_set.current_version,
            )
        raise api_error(
            404,
            "RULE_SET_VERSION_NOT_FOUND",
            f"rule set {code!r} has no version {version}",
            current_version=rule_set.current_version,
        )
    if jurisdiction is not None and rule_set.jurisdiction != jurisdiction:
        raise api_error(
            422,
            "RULE_SET_JURISDICTION_MISMATCH",
            f"rule set {code!r} applies to {rule_set.jurisdiction!r}, "
            f"not {jurisdiction!r}",
        )
    return rule_set, snapshot
