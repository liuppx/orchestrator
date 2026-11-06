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
    """Providers are whatever Runtime Adapters the registered Hubs reported.

    A runtime is offered when at least one Hub has reported its adapter; the Hubs
    that reported it are listed so the console can offer a placement choice.
    """
    adapters: dict[str, dict[str, object]] = {}
    hub_ids: dict[str, list[str]] = {}
    if settings.database_url:
        try:
            hub_service = MachineHubService(settings.database_url)
            for hub in hub_service.list_runtime_hubs():
                hub_id = str(hub["id"])
                for adapter in hub_service.adapter_catalog(hub_id):
                    runtime_type = str(adapter["runtime_type"])
                    capabilities = list(adapter["capabilities"])  # type: ignore[arg-type]
                    known = adapters.get(runtime_type)
                    if known is None or len(capabilities) > len(known["capabilities"]):  # type: ignore[arg-type]
                        adapters[runtime_type] = {
                            "capabilities": capabilities,
                            "runtime_version": adapter.get("runtime_version"),
                        }
                    hosts = hub_ids.setdefault(runtime_type, [])
                    if hub_id not in hosts:
                        hosts.append(hub_id)
        except RuntimeError:
            adapters = {}
            hub_ids = {}
    ordered = ["openclaw", *[key for key in adapters if key != "openclaw"]]
    items: list[RuntimeProviderView] = []
    for runtime_type in ordered:
        adapter = adapters.get(runtime_type)
        capabilities = list(adapter["capabilities"]) if adapter else []  # type: ignore[index]
        hosts = sorted(hub_ids.get(runtime_type, []))
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
                hub_ids=hosts,
            )
        )
    return RuntimeProviderListResponse(items=items)
