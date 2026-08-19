import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from omai.api.app import create_app


# Compatibility entry point for `uvicorn api:app`; service units use the factory
# directly, while local operators can still use this module.
app = create_app()
