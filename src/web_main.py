from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    uvicorn.run("src.web.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
