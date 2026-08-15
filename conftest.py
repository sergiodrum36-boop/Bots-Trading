"""Permite que `pytest` encuentre los paquetes del proyecto (connectors/, etc.)
sin necesidad de instalar el proyecto como paquete todavia."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
