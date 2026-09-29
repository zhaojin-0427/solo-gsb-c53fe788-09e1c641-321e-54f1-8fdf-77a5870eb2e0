import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite in tests).
JSON_TYPE = JSONB().with_variant(sa.JSON(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Ingredient(Base):
    """An immutable raw material with its allergen markers."""

    __tablename__ = "ingredients"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    # [{"allergen": "gluten", "concentration": 10.0, "unit": "%"}, ...]
    allergens = sa.Column(JSON_TYPE, nullable=False, default=list)
    # ["peanut", "sesame", ...] — "may contain" traces
    cross_contact = sa.Column(JSON_TYPE, nullable=False, default=list)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)


class Rule(Base):
    """A jurisdiction threshold rule. Immutable once published."""

    __tablename__ = "rules"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    jurisdiction = sa.Column(sa.String(32), nullable=False, index=True)
    allergen = sa.Column(sa.String(64), nullable=False)
    threshold = sa.Column(sa.Float, nullable=False)
    unit = sa.Column(sa.String(16), nullable=False)
    action = sa.Column(sa.String(16), nullable=False)  # declare | warning | prohibit
    applies_to = sa.Column(sa.String(16), nullable=False)  # contains | cross_contact
    status = sa.Column(sa.String(16), nullable=False, default="draft")  # draft | published
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    published_at = sa.Column(sa.DateTime(timezone=True), nullable=True)


class Recipe(Base):
    __tablename__ = "recipes"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    versions = sa.orm.relationship(
        "RecipeVersion", back_populates="recipe", order_by="RecipeVersion.version"
    )


class RecipeVersion(Base):
    """A versioned, optionally nested recipe. Immutable once published."""

    __tablename__ = "recipe_versions"
    __table_args__ = (sa.UniqueConstraint("recipe_id", "version", name="uq_recipe_version"),)

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    recipe_id = sa.Column(sa.Uuid, sa.ForeignKey("recipes.id"), nullable=False, index=True)
    version = sa.Column(sa.Integer, nullable=False)
    status = sa.Column(sa.String(16), nullable=False, default="draft")  # draft | published
    # [{"kind": "ingredient"|"recipe", "code": ..., "version": int|null,
    #   "quantity": float, "unit": str}, ...]
    items = sa.Column(JSON_TYPE, nullable=False, default=list)
    yield_quantity = sa.Column(sa.Float, nullable=True)
    yield_unit = sa.Column(sa.String(16), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    published_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    recipe = sa.orm.relationship("Recipe", back_populates="versions")


class Evaluation(Base):
    """A stored, replayable evaluation result keyed by an idempotency key."""

    __tablename__ = "evaluations"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key = sa.Column(sa.String(128), unique=True, nullable=False, index=True)
    request_hash = sa.Column(sa.String(64), nullable=False)
    recipe_code = sa.Column(sa.String(64), nullable=False)
    recipe_version = sa.Column(sa.Integer, nullable=False)
    jurisdiction = sa.Column(sa.String(32), nullable=False)
    status = sa.Column(sa.String(16), nullable=False, default="completed")
    result = sa.Column(JSON_TYPE, nullable=False, default=dict)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
