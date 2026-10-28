from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.api import internal_router, public_router
from orchestrator.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Orchestrator", version="0.1.0")

    app.include_router(internal_router)
    app.include_router(public_router)

    ui_dir = settings.ui_dir
    index_file = ui_dir / "index.html"
    if ui_dir.exists() and index_file.exists():
        assets_dir = ui_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(index_file)

        @app.get("/{full_path:path}")
        async def spa_entry(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")

            candidate = (ui_dir / full_path).resolve()
            try:
                candidate.relative_to(ui_dir.resolve())
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="not found") from exc

            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_file)

    return app


def main() -> None:
    settings = Settings()
    host, port = settings.bind_addr.split(":")
    uvicorn.run(
        "orchestrator.app:create_app",
        factory=True,
        host=host,
        port=int(port),
        reload=False,
    )


if __name__ == "__main__":
    main()
