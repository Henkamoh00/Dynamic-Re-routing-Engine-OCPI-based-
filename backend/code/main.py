from pathlib import Path

from api.endpoints import router
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from services.cpo_repository import DataSourceError, describe_data_source

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(
    title="FleetRoute-OCPI Dynamic Re-routing Engine",
    description="Dynamic re-routing and automated rescheduling for electric trucks (OCPI 2.2.1).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health", tags=["system"])
def health_check() -> dict:
    return {"status": "ok"}


@app.get("/health/data", tags=["system"])
def data_source_health() -> dict:
    try:
        return describe_data_source()
    except DataSourceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


if FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")