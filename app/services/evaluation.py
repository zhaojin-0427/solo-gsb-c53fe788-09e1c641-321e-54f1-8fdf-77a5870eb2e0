"""Evaluation engine.

Aggregates allergen masses over the expanded leaf set, converts them to
final-product concentrations, accumulates cross-contact traces, and matches
everything against the published rules of a jurisdiction. All output lists
are deterministically ordered so results are stable and diffable.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from ..errors import api_error
from ..models import Ingredient, Recipe, RecipeVersion, Rule
from ..units import concentration_to_ppm
from .expansion import CycleError, expand_root


def _r6(x: float) -> float:
    return round(x, 6)


def _classify(action: str, entry: dict, obligations: list, warnings: list, violations: list) -> None:
    if action == "declare":
        obligations.append(entry)
    elif action == "warning":
        warnings.append(entry)
    else:  # prohibit
        violations.append(entry)


def _load_rules(db: Session, jurisdiction: str) -> list[dict]:
    """Legacy behavior: all published standalone rules of a jurisdiction."""
    rows = (
        db.query(Rule)
        .filter(Rule.jurisdiction == jurisdiction, Rule.status == "published")
        .order_by(Rule.code)
        .all()
    )
    return [
        {
            "code": rule.code,
            "allergen": rule.allergen,
            "threshold_ppm": concentration_to_ppm(rule.threshold, rule.unit),
            "action": rule.action,
            "applies_to": rule.applies_to,
        }
        for rule in rows
    ]


def evaluate_recipe(
    db: Session,
    recipe_code: str,
    recipe_version: int,
    jurisdiction: str,
    binding: dict | None = None,
) -> dict:
    """Evaluate a published recipe version.

    When ``binding`` is ``None`` the published standalone rules of the
    jurisdiction are used (legacy behavior). Otherwise ``binding`` is an
    immutable rule-set snapshot ``{"code", "version", "rules"}`` and only that
    frozen rule list is applied, so replaying the binding always reproduces the
    same verdict.
    """
    recipe = db.query(Recipe).filter(Recipe.code == recipe_code).first()
    if recipe is None:
        raise api_error(
            404,
            "RECIPE_NOT_FOUND",
            f"unknown recipe: {recipe_code!r}",
            missing=[{"type": "recipe", "code": recipe_code, "reason": "unknown_recipe"}],
        )
    rv = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.recipe_id == recipe.id, RecipeVersion.version == recipe_version)
        .first()
    )
    if rv is None:
        raise api_error(
            404,
            "RECIPE_VERSION_NOT_FOUND",
            f"recipe {recipe_code!r} has no version {recipe_version}",
            missing=[{"type": "recipe_version", "code": recipe_code,
                      "version": recipe_version, "reason": "unknown_version"}],
        )
    if rv.status != "published":
        raise api_error(
            422,
            "VERSION_NOT_PUBLISHED",
            f"recipe {recipe_code!r} version {recipe_version} is not published",
        )

    try:
        expansion = expand_root(db, rv, recipe_code)
    except CycleError as exc:
        raise api_error(422, "RECIPE_CYCLE", "recipe graph contains a cycle", cycle=exc.cycle)

    if expansion.missing:
        raise api_error(
            422,
            "INCOMPLETE_INPUT",
            "evaluation input is incomplete; supply the minimal missing set",
            missing=[m.as_dict() for m in expansion.missing],
        )
    if expansion.yield_g <= 0:
        raise api_error(422, "EMPTY_RECIPE", "recipe total mass is zero")

    codes = sorted({leaf.ingredient_code for leaf in expansion.leaves})
    ingredients = (
        {i.code: i for i in db.query(Ingredient).filter(Ingredient.code.in_(codes)).all()}
        if codes
        else {}
    )

    total = expansion.yield_g
    contains_mass: dict[str, float] = defaultdict(float)
    contains_paths: dict[str, list[dict]] = defaultdict(list)
    cross_paths: dict[str, list[dict]] = defaultdict(list)

    for leaf in expansion.leaves:
        ing = ingredients[leaf.ingredient_code]
        for content in ing.allergens:
            fraction = concentration_to_ppm(content["concentration"], content["unit"]) / 1e6
            allergen_mass = leaf.mass_g * fraction
            if allergen_mass <= 0:
                continue
            allergen = content["allergen"]
            contains_mass[allergen] += allergen_mass
            contains_paths[allergen].append(
                {
                    "path": leaf.path,
                    "ingredient": leaf.ingredient_code,
                    "ingredient_mass_g": _r6(leaf.mass_g),
                    "allergen_mass_g": _r6(allergen_mass),
                    "contribution_ppm": _r6(allergen_mass / total * 1e6),
                }
            )
        for cc in ing.cross_contact:
            cross_paths[cc].append(
                {
                    "path": leaf.path,
                    "ingredient": leaf.ingredient_code,
                    "ingredient_mass_g": _r6(leaf.mass_g),
                }
            )

    # Stable path ordering within each allergen.
    for paths in list(contains_paths.values()) + list(cross_paths.values()):
        paths.sort(key=lambda p: ("/".join(p["path"]), p["ingredient"]))

    if binding is None:
        rules = _load_rules(db, jurisdiction)
        rule_set_block: dict | None = None
    else:
        # Replay against the frozen snapshot; standalone rules are ignored.
        rules = sorted(binding["rules"], key=lambda r: r["code"])
        rule_set_block = {"code": binding["code"], "version": binding["version"]}

    obligations: list[dict] = []
    warnings: list[dict] = []
    violations: list[dict] = []
    regulated: set[str] = set()

    for rule in rules:
        threshold_ppm = rule["threshold_ppm"]
        if rule["applies_to"] == "contains":
            mass = contains_mass.get(rule["allergen"])
            if mass is None:
                continue
            regulated.add(rule["allergen"])
            conc_ppm = mass / total * 1e6
            if conc_ppm < threshold_ppm:
                continue
            entry = {
                "allergen": rule["allergen"],
                "rule_code": rule["code"],
                "action": rule["action"],
                "source": "contains",
                "threshold_ppm": _r6(threshold_ppm),
                "concentration_ppm": _r6(conc_ppm),
                "trigger_paths": contains_paths[rule["allergen"]],
            }
            _classify(rule["action"], entry, obligations, warnings, violations)
        else:  # cross_contact
            paths = cross_paths.get(rule["allergen"])
            if not paths:
                continue
            regulated.add(rule["allergen"])
            entry = {
                "allergen": rule["allergen"],
                "rule_code": rule["code"],
                "action": rule["action"],
                "source": "cross_contact",
                "threshold_ppm": _r6(threshold_ppm),
                "trigger_paths": paths,
            }
            _classify(rule["action"], entry, obligations, warnings, violations)

    sort_key = lambda e: (e["allergen"], e["rule_code"])  # noqa: E731
    obligations.sort(key=sort_key)
    warnings.sort(key=sort_key)
    violations.sort(key=sort_key)

    present = set(contains_mass) | set(cross_paths)
    unregulated = sorted(present - regulated)

    result = {
        "recipe": {"code": recipe_code, "version": recipe_version},
        "jurisdiction": jurisdiction,
        "status": "completed",
        "total_mass_g": _r6(total),
        "resolved_recipes": expansion.resolved_recipes,
        "allergens": [
            {
                "allergen": a,
                "concentration_ppm": _r6(contains_mass[a] / total * 1e6),
                "concentration_percent": _r6(contains_mass[a] / total * 100.0),
            }
            for a in sorted(contains_mass)
        ],
        "obligations": obligations,
        "warnings": warnings,
        "violations": violations,
        "unregulated_allergens": unregulated,
    }
    if rule_set_block is not None:
        result["rule_set"] = rule_set_block
    return result
