"""Configure imports for statusline tests.

The statusline directory contains statusline.py (a module, not a package).
Python 3.3+ treats directories without __init__.py as namespace packages,
so 'import statusline' finds the directory, not statusline.py.
We explicitly load the .py file and register it in sys.modules.
"""

import importlib.util
import sys
from pathlib import Path

_module_path = Path(__file__).resolve().parent.parent / "statusline.py"
_spec = importlib.util.spec_from_file_location("statusline", str(_module_path))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["statusline"] = _mod
_spec.loader.exec_module(_mod)
