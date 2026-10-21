"""Write the API description: `python -m tenant_service.export_openapi openapi.json` (no network or database needed)."""

import json
import sys

from .app import create_app
from .config import Settings


def main(path: str = "openapi.json") -> None:
    settings = Settings(issuer="https://auth.example/realms/taraz", tenant_id="taraz", tenant_name="Taraz",
                        client_id="tenant-service", client_secret="unused", database_url="sqlite://")
    app = create_app(settings, run_sync_loop=False)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(app.openapi(), f, indent=2)
        f.write("\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main(*sys.argv[1:])
