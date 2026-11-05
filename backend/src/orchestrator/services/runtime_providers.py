from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.models import RuntimeProviderListResponse, RuntimeProviderView
from orchestrator.services.hubs import MachineHubService

PROVIDER_NAMES = {
    "openclaw": "OpenClaw",
    "codex": "Codex CLI",
    "claude-code": "Claude Code CLI",
}


def runtime_providers(settings: Settings) -> RuntimeProviderListResponse:
    """Providers are whatever Runtime Adapters the configured Hub reported; OpenClaw is always listed."""
    adapters: list[dict[str, object]] = []
    if settings.hub_id and settings.database_url:
        try:
            adapters = MachineHubService(settings.database_url).adapter_catalog(settings.hub_id)
        except RuntimeError:
            adapters = []
    by_type = {str(item["runtime_type"]): item for item in adapters}
    ordered = ["openclaw", *[key for key in by_type if key != "openclaw"]]
    items: list[RuntimeProviderView] = []
    for runtime_type in ordered:
        adapter = by_type.get(runtime_type)
        capabilities = list(adapter["capabilities"]) if adapter else []  # type: ignore[index]
        if adapter is not None and not capabilities:
            # The Hub attached the adapter but the CLI itself is missing on that machine.
            availability = "not-installed"
        else:
            availability = "configured" if capabilities else "not-configured"
        items.append(
            RuntimeProviderView(
                id=runtime_type,
                name=PROVIDER_NAMES.get(runtime_type, runtime_type),
                category="agent-runtime",
                management_mode="machine-hub",
                availability=availability,
                capabilities=capabilities,
            )
        )
    return RuntimeProviderListResponse(items=items)
