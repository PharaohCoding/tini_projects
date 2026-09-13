"""python -m backend — bind 127.0.0.1 only."""

from __future__ import annotations

import uvicorn

from backend.main import app
from backend.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        factory=False,
    )


if __name__ == "__main__":
    main()
