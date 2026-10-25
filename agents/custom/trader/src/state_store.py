from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.state_path = runtime_dir / "state.json"

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"strategies": {}}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save(self, payload: dict[str, Any]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
