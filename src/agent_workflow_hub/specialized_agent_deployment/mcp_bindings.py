"""Shared explicit MCP bindings; business entrypoints belong to their Skills."""
from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Mapping
from .contracts import DeploymentRequest
from .runtime_bundle import effective_mcp_servers


def mcp_servers(request: DeploymentRequest, *, deployed: bool = False, error_type=ValueError) -> tuple[dict, ...]:
    entries = request.host_options.get("mcp_servers", ())
    if not isinstance(entries, (tuple, list)):
        raise error_type("mcp_servers must be an array")
    selected = {request.primary_workflow, *(s.name for s in request.related_workflows),
                *(s.name for s in request.auxiliary_skills)}
    names: set[str] = set()
    result = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"workflow", "server_name", "command", "args", "cwd"}:
            raise error_type("MCP binding requires workflow, server_name, command, args, cwd only")
        if not isinstance(entry["workflow"], str) or entry["workflow"] not in selected:
            raise error_type("MCP workflow must be explicitly selected")
        name = entry["server_name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or name in names:
            raise error_type("MCP server_name must be valid and unique")
        for field in ("command", "cwd"):
            value = entry[field]
            if not isinstance(value, str) or "\x00" in value or not Path(value).is_absolute():
                raise error_type(f"MCP {field} must be an absolute path")
        args = entry["args"]
        if not isinstance(args, (tuple, list)) or not all(isinstance(arg, str) and "\x00" not in arg for arg in args):
            raise error_type("MCP args must be a string array")
        names.add(name)
        result.append({**entry, "args": list(args)})
    servers = tuple(result)
    return effective_mcp_servers(request, servers) if deployed else servers
