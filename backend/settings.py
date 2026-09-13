from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass
class Settings:
    data_dir: Path
    max_running: int = 2
    policy: str = "fair-kind"
    auto_summary: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    duration_research: float = 4.0
    duration_implement: float = 10.0
    duration_test: float = 6.0
    duration_generic: float = 8.0

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("TINI_DATA_DIR", "data")).resolve()
        max_running = int(os.getenv("TINI_MAX_RUNNING", "2"))
        host = os.getenv("TINI_HOST", "127.0.0.1")
        port = int(os.getenv("TINI_PORT", "8000"))
        return cls(
            data_dir=data_dir,
            max_running=max(1, max_running),
            auto_summary=_env_bool("TINI_AUTO_SUMMARY", True),
            host=host,
            port=port,
            duration_research=_env_float("TINI_DURATION_RESEARCH", 4.0),
            duration_implement=_env_float("TINI_DURATION_IMPLEMENT", 10.0),
            duration_test=_env_float("TINI_DURATION_TEST", 6.0),
            duration_generic=_env_float("TINI_DURATION_GENERIC", 8.0),
        )

    def duration_for(self, kind: str) -> float:
        mapping = {
            "research": self.duration_research,
            "implement": self.duration_implement,
            "test": self.duration_test,
            "generic": self.duration_generic,
        }
        return max(0.0, mapping.get(kind, self.duration_generic))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tini.db"
