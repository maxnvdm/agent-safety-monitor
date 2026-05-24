"""FastAPI app serving session + result data to the Vue frontend."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import results, sessions

app = FastAPI(title="Coding Agent Safety Monitor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
app.include_router(results.router, prefix="/results", tags=["results"])


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
