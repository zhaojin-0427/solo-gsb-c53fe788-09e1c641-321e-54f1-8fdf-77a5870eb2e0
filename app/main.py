from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from .db import Base, engine
from .routers import evaluations, ingredients, recipes, rule_sets, rules
from .units import CONCENTRATION_TO_PPM, MASS_TO_GRAMS


def _ensure_backfill_columns() -> None:
    """Idempotent additive bootstrap for pre-existing databases.

    ``create_all`` creates new tables (rule_sets*) but cannot add columns to an
    existing table; older deployments would otherwise miss the snapshot-binding
    columns on ``evaluations``.
    """
    inspector = inspect(engine)
    if "evaluations" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("evaluations")}
    statements = []
    if "rule_set_code" not in existing:
        statements.append("ALTER TABLE evaluations ADD COLUMN rule_set_code VARCHAR(64)")
    if "rule_set_version" not in existing:
        statements.append("ALTER TABLE evaluations ADD COLUMN rule_set_version INTEGER")
    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_backfill_columns()
    yield


app = FastAPI(
    title="Food Allergen Recipe Compliance API",
    version="1.0.0",
    description=(
        "Register immutable ingredients, jurisdiction threshold rules and versioned "
        "nested recipes; evaluate them for allergen labeling obligations, warnings "
        "and threshold violations with idempotent, replayable requests."
    ),
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    try:
        errors = jsonable_encoder(exc.errors())
    except Exception:  # pragma: no cover - defensive
        errors = [{"msg": str(exc)}]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": "request validation failed",
                "errors": errors,
            }
        },
    )


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "food-allergen-compliance",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": [
            "/ingredients",
            "/rules",
            "/rule-sets",
            "/recipes",
            "/evaluations",
            "/units",
            "/health",
        ],
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/units", tags=["meta"])
def supported_units():
    return {
        "mass_units": sorted(MASS_TO_GRAMS),
        "concentration_units": sorted(CONCENTRATION_TO_PPM),
    }


app.include_router(ingredients.router)
app.include_router(rules.router)
app.include_router(rule_sets.router)
app.include_router(recipes.router)
app.include_router(evaluations.router)
