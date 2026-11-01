from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.models import RuntimeProviderListResponse, RuntimeProviderView
from orchestrator.services.hubs import MachineHubService


def runtime_providers(settings: Settings) -> RuntimeProviderListResponse:
    capabilities: list[str] = []
    if settings.hub_id and settings.database_url:
        try:
            capabilities = MachineHubService(settings.database_url).adapter_capabilities(settings.hub_id, "openclaw")
        except RuntimeError:
            capabilities = []
    return RuntimeProviderListResponse(
        items=[
            RuntimeProviderView(
                id="openclaw",
                name="OpenClaw",
                category="agent-runtime",
                management_mode="machine-hub",
                availability="configured" if capabilities else "not-configured",
                capabilities=capabilities,
            )
        ]
    )
