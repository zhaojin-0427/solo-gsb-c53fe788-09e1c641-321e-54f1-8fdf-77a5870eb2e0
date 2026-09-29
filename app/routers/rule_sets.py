from fastapi import APIRouter, Depends
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import Evaluation, RuleSet, RuleSetVersion, utcnow
from ..schemas import RuleSetCreate, RuleSetSubmit, normalize_code
from ..services.rule_sets import find_conflicts, normalize_entries, summary_of

router = APIRouter(prefix="/rule-sets", tags=["rule-sets"])


def _get_rule_set_or_404(db: Session, code: str) -> RuleSet:
    rule_set = db.query(RuleSet).filter(RuleSet.code == normalize_code(code)).first()
    if rule_set is None:
        raise api_error(404, "RULE_SET_NOT_FOUND", f"unknown rule set: {code!r}")
    return rule_set


def serialize_draft(rs: RuleSet) -> dict:
    return {
        "code": rs.code,
        "jurisdiction": rs.jurisdiction,
        "name": rs.name,
        "status": "draft",
        "current_version": rs.current_version,
        "rules": rs.draft_rules or [],
        "created_at": rs.created_at.isoformat() if rs.created_at else None,
        "updated_at": rs.updated_at.isoformat() if rs.updated_at else None,
    }


def serialize_published(rs: RuleSetVersion) -> dict:
    return {
        "code": rs.rule_set.code,
        "jurisdiction": rs.rule_set.jurisdiction,
        "status": "published",
        "version": rs.version,
        "rules": rs.rules,
        "summary": rs.summary,
        "published_at": rs.published_at.isoformat() if rs.published_at else None,
    }


@router.post("", status_code=201)
def create_rule_set(body: RuleSetCreate, db: Session = Depends(get_db)):
    """Create an empty per-jurisdiction rule-set draft at base_version 0."""
    if db.query(RuleSet.id).filter(RuleSet.code == body.code).first():
        raise api_error(
            409, "RULE_SET_EXISTS", f"rule set {body.code!r} already exists"
        )
    rs = RuleSet(
        code=body.code,
        jurisdiction=body.jurisdiction,
        name=body.name,
        current_version=0,
        draft_rules=None,
    )
    db.add(rs)
    db.commit()
    db.refresh(rs)
    return serialize_draft(rs)


@router.get("")
def list_rule_sets(db: Session = Depends(get_db)):
    rows = db.query(RuleSet).order_by(RuleSet.code).all()
    return [serialize_draft(rs) for rs in rows]


@router.get("/{code}")
def get_rule_set(code: str, db: Session = Depends(get_db)):
    return serialize_draft(_get_rule_set_or_404(db, code))


@router.put("/{code}")
def submit_rule_set(code: str, body: RuleSetSubmit, db: Session = Depends(get_db)):
    """Submit (replace) the draft with ppm-normalized thresholds.

    Optimistic concurrency: ``base_version`` must equal ``current_version``
    (initially 0). On mismatch a 409 carrying ``current_version`` is returned
    and nothing is written. A successful submit increments the version.
    """
    rs = _get_rule_set_or_404(db, code)
    rules = normalize_entries(body.rules)
    # Atomic compare-and-swap so two concurrent submits at the same
    # base_version cannot both succeed; rowcount == 0 means we lost the race.
    result = db.execute(
        update(RuleSet)
        .where(RuleSet.id == rs.id, RuleSet.current_version == body.base_version)
        .values(
            draft_rules=rules,
            current_version=RuleSet.current_version + 1,
            updated_at=utcnow(),
        )
    )
    if result.rowcount == 0:
        db.rollback()
        current = _get_rule_set_or_404(db, code)
        raise api_error(
            409,
            "VERSION_CONFLICT",
            f"base_version {body.base_version} does not match current_version "
            f"{current.current_version}; refetch the draft and resubmit",
            current_version=current.current_version,
        )
    db.commit()
    db.refresh(rs)
    return serialize_draft(rs)


@router.post("/{code}/publish")
def publish_rule_set(code: str, db: Session = Depends(get_db)):
    """Freeze the current draft into an immutable rule_set_version.

    Rejected (422) when the draft is empty or contains overlapping/conflicting
    rules. Publishing the already-frozen version is idempotent: if no newer
    submit followed the last publish, the same snapshot is returned.
    """
    rs = _get_rule_set_or_404(db, code)
    rules = rs.draft_rules
    if not rules:
        raise api_error(
            422,
            "RULE_SET_EMPTY",
            f"rule set {rs.code!r} has no submitted draft; submit rules first",
        )

    latest = (
        db.query(RuleSetVersion)
        .filter(RuleSetVersion.rule_set_id == rs.id)
        .order_by(RuleSetVersion.version.desc())
        .first()
    )
    # Idempotent: the draft at this version was already published.
    if latest is not None and latest.version == rs.current_version:
        return serialize_published(latest)

    conflicts = find_conflicts(rules)
    if conflicts:
        raise api_error(
            409,
            "RULE_SET_CONFLICT",
            f"rule set {rs.code!r} has {len(conflicts)} conflicting rule(s); "
            "publishing is rejected",
            conflicts=conflicts,
        )

    snapshot = RuleSetVersion(
        rule_set_id=rs.id,
        version=rs.current_version,
        rules=rules,
        summary=summary_of(rules),
    )
    db.add(snapshot)
    try:
        db.commit()
    except IntegrityError:  # concurrent publish raced to the same version
        db.rollback()
        existing = (
            db.query(RuleSetVersion)
            .filter(
                RuleSetVersion.rule_set_id == rs.id,
                RuleSetVersion.version == rs.current_version,
            )
            .first()
        )
        if existing is not None:
            return serialize_published(existing)
        raise
    return serialize_published(snapshot)


@router.get("/{code}/versions")
def list_rule_set_versions(code: str, db: Session = Depends(get_db)):
    rs = _get_rule_set_or_404(db, code)
    rows = (
        db.query(RuleSetVersion)
        .filter(RuleSetVersion.rule_set_id == rs.id)
        .order_by(RuleSetVersion.version)
        .all()
    )
    return [
        {
            "code": rs.code,
            "version": row.version,
            "summary": row.summary,
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }
        for row in rows
    ]


@router.get("/{code}/versions/{version}")
def get_rule_set_version(code: str, version: int, db: Session = Depends(get_db)):
    rs = _get_rule_set_or_404(db, code)
    snapshot = (
        db.query(RuleSetVersion)
        .filter(RuleSetVersion.rule_set_id == rs.id, RuleSetVersion.version == version)
        .first()
    )
    if snapshot is None:
        raise api_error(
            404,
            "RULE_SET_VERSION_NOT_FOUND",
            f"rule set {rs.code!r} has no published version {version}",
            current_version=rs.current_version,
        )
    return serialize_published(snapshot)


@router.get("/{code}/versions/{version}/evaluations")
def list_evaluations_for_version(code: str, version: int, db: Session = Depends(get_db)):
    """Fixed replay listing: every evaluation bound to this exact snapshot.

    Each item always carries the rule-set summary, the bound recipe version and
    the stored result — the immutable snapshot guarantees identical verdicts.
    """
    rs = _get_rule_set_or_404(db, code)
    snapshot = (
        db.query(RuleSetVersion)
        .filter(RuleSetVersion.rule_set_id == rs.id, RuleSetVersion.version == version)
        .first()
    )
    if snapshot is None:
        raise api_error(
            404,
            "RULE_SET_VERSION_NOT_FOUND",
            f"rule set {rs.code!r} has no published version {version}",
            current_version=rs.current_version,
        )
    evals = (
        db.query(Evaluation)
        .filter(
            Evaluation.rule_set_code == rs.code,
            Evaluation.rule_set_version == version,
        )
        .order_by(Evaluation.created_at, Evaluation.id)
        .all()
    )
    return [
        {
            "evaluation_id": str(ev.id),
            "rule_set": {"code": rs.code, "version": version, "summary": snapshot.summary},
            "recipe": {"code": ev.recipe_code, "version": ev.recipe_version},
            "jurisdiction": ev.jurisdiction,
            "replayed": False,
            "result": ev.result,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        }
        for ev in evals
    ]
