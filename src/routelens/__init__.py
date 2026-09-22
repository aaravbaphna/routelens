"""RouteLens: see why LiteLLM picked each model, on every turn.

    litellm_settings:
      callbacks: routelens.instance
"""
from __future__ import annotations

from typing import Any

__version__ = "0.1.0"
__all__ = ["RouteLens", "instance", "__version__"]


def __getattr__(name: str) -> Any:
    # Import lazily so `import routelens` has no side effects; `routelens.instance`
    # creates the callback (opens the DB, mounts the dashboard) on first access.
    global _instance
    if name == "RouteLens":
        from .callback import RouteLens
        return RouteLens
    if name == "instance":
        if _instance is None:
            from .callback import RouteLens
            _instance = RouteLens()
        return _instance
    raise AttributeError(name)


_instance = None
