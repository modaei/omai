import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from omai.api.app import app
