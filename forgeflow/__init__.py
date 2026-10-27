"""ForgeFlow: a human-guided, multi-model software factory (pre-alpha, Checkpoints A-C)."""

import sys

__version__ = "0.1.0.dev0"

# Keep in sync with requires-python in pyproject.toml (a test enforces it). This module uses
# only syntax that older Pythons parse, so they get this message rather than a SyntaxError
# from a module further down.
MIN_PYTHON = (3, 13)

if sys.version_info < MIN_PYTHON:
    _need = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    raise ImportError(
        f"ForgeFlow requires Python {_need} or newer, but {sys.executable} is Python "
        f"{sys.version.split()[0]}. On Windows run it as `py -{_need} -m forgeflow`; "
        f"elsewhere use a python{_need} executable."
    )
