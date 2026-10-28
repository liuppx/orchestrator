from __future__ import annotations

import shutil

from orchestrator.config import Settings
from orchestrator.models import RuntimeProviderListResponse, RuntimeProviderView


def runtime_providers(settings: Settings) -> RuntimeProviderListResponse:
    executable_available = bool(settings.openclaw_prefix) or shutil.which("openclaw") is not None
    return RuntimeProviderListResponse(
        items=[
            RuntimeProviderView(
                id="openclaw",
                name="OpenClaw",
                category="agent-runtime",
                management_mode="local-process",
                availability="configured" if executable_available else "not-configured",
                capabilities=["create", "start", "stop", "logs", "diagnose", "channel-pairing"],
            )
        ]
    )
