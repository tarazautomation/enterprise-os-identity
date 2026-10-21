"""Entry point for uvicorn: `uvicorn tenant_service.main:app`. Reads settings from the environment."""

import logging
import sys

from .app import create_app
from .config import Settings

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)       # don't log every Keycloak call of the 30 s sync
app = create_app(Settings.from_env())
