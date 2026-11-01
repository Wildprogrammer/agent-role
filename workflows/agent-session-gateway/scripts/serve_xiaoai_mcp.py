from __future__ import annotations

import asyncio
import sys

from agent_workflow_hub.xiaozhi_hermes_bridge.xiaoai_mcp import (
    XiaoAiMcpConfigurationError,
    XiaoAiMcpSettings,
    serve_xiaoai_mcp,
)


def main() -> int:
    try:
        settings = XiaoAiMcpSettings.from_environment()
        asyncio.run(serve_xiaoai_mcp(settings))
    except (XiaoAiMcpConfigurationError, OSError) as exc:
        print(f"XiaoAi MCP startup rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
