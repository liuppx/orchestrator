from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass
class DsaBridgeConfig:
    root_dir: Path
    python_bin: str
    market: str
    use_alphasift: bool
    timeout_seconds: int
    screening_strategy: str
    screening_max_results: int
    hot_stock_count: int
    limit_up_count: int
    concept_top_n: int
    sector_top_n: int
    history_days: int
    enable_market_stats: bool
    enable_hot_stocks: bool
    enable_limit_up_pool: bool
    enable_sector_rankings: bool
    enable_concept_rankings: bool


class DsaBridge:
    def __init__(self, config: DsaBridgeConfig, trader_root: Path) -> None:
        self.config = config
        self.trader_root = trader_root

    @property
    def worker_script(self) -> Path:
        return self.trader_root / "src" / "providers" / "dsa_worker.py"

    def build_cycle_bundle(self, strategies: list[dict[str, Any]], run_day: date) -> dict[str, Any]:
        payload = {
            "dsa_root": str(self.config.root_dir),
            "market": self.config.market,
            "use_alphasift": self.config.use_alphasift,
            "screening_strategy": self.config.screening_strategy,
            "screening_max_results": self.config.screening_max_results,
            "hot_stock_count": self.config.hot_stock_count,
            "limit_up_count": self.config.limit_up_count,
            "concept_top_n": self.config.concept_top_n,
            "sector_top_n": self.config.sector_top_n,
            "history_days": self.config.history_days,
            "enable_market_stats": self.config.enable_market_stats,
            "enable_hot_stocks": self.config.enable_hot_stocks,
            "enable_limit_up_pool": self.config.enable_limit_up_pool,
            "enable_sector_rankings": self.config.enable_sector_rankings,
            "enable_concept_rankings": self.config.enable_concept_rankings,
            "run_day": run_day.isoformat(),
            "strategies": strategies,
        }

        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            payload_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")

        try:
            try:
                completed = subprocess.run(
                    [self.config.python_bin, str(self.worker_script), "--payload", str(payload_path)],
                    cwd=self.trader_root,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"DSA bridge timed out after {self.config.timeout_seconds}s"
                ) from exc
        finally:
            payload_path.unlink(missing_ok=True)

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = stderr or stdout or f"worker exited with code {completed.returncode}"
            raise RuntimeError(f"DSA bridge failed: {detail}")

        stdout = completed.stdout.strip()
        candidate_text = stdout
        if "\n" in stdout:
            non_empty_lines = [line for line in stdout.splitlines() if line.strip()]
            if non_empty_lines:
                candidate_text = non_empty_lines[-1]
        try:
            result = json.loads(candidate_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DSA bridge returned invalid JSON: {exc}") from exc

        if not isinstance(result, dict):
            raise RuntimeError("DSA bridge returned non-object payload")

        if result.get("ok") is False:
            errors = result.get("errors")
            if isinstance(errors, list) and errors:
                detail = "; ".join(str(item) for item in errors)
            else:
                detail = "unknown DSA bridge error"
            raise RuntimeError(detail)

        return result
