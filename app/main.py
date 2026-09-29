from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import Base, engine
from .routers import evaluations, ingredients, recipes, rules
from .units import CONCENTRATION_TO_PPM, MASS_TO_GRAMS


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
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
        "endpoints": ["/ingredients", "/rules", "/recipes", "/evaluations", "/units", "/health"],
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
app.include_router(recipes.router)
app.include_router(evaluations.router)
