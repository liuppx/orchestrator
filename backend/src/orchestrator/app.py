from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from orchestrator.api import internal_router, public_router
from orchestrator.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Orchestrator", version="0.1.0")

    app.include_router(internal_router)
    app.include_router(public_router)

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
