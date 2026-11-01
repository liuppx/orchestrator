from __future__ import annotations

import json
from pathlib import Path

from orchestrator.protocols.hub_v1 import hub_protocol_json_schema


def main() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    output_path = repository_root / "docs" / "protocols" / "hub-v1.schema.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(hub_protocol_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
