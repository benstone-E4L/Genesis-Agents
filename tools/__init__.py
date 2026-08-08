"""Tool registry for the agent runtime. Tools are async callables returning JSON-serializable dicts."""
from __future__ import annotations
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

_TOOLS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {}


def register_tool(name: str, fn: Callable[..., Awaitable[dict[str, Any]]], schema: dict[str, Any]) -> None:
    """Register a tool by name with its JSON schema."""
    _TOOLS[name] = fn
    _TOOL_SCHEMAS[name] = schema


def get_tool(name: str) -> Callable[..., Awaitable[dict[str, Any]]] | None:
    return _TOOLS.get(name)


def tool_schemas_for(allowed: list[str]) -> list[dict[str, Any]]:
    """Return OpenAI-format tool definitions for the allowed tool names."""
    return [_TOOL_SCHEMAS[n] for n in allowed if n in _TOOL_SCHEMAS]


def register_default_tools() -> None:
    """Idempotent - auto-discover every *_tool.py module and call its register() function."""
    import importlib
    from pathlib import Path

    tools_dir = Path(__file__).parent
    for entry in sorted(tools_dir.glob("*_tool.py")):
        module_name = entry.stem
        try:
            mod = importlib.import_module(f".{module_name}", package=__name__)
            if hasattr(mod, "register"):
                mod.register()
                log.debug("registered tool module: %s", module_name)
        except Exception:
            log.exception("failed to register tool module %s", module_name)
