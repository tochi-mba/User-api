"""The server entry point.

``uvicorn`` is invoked from here rather than from a shell command so the host and port come
from the same configuration as everything else, and so a misspelled ``USER_API_``-prefixed
variable fails here -- loudly, at startup -- rather than leaving a setting on its default.
"""

from __future__ import annotations

import uvicorn

from user_api.core.config import load_settings


def main() -> None:
    """Serve the API."""
    settings = load_settings()
    uvicorn.run(
        "user_api.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
