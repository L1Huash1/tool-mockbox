"""Launch the real optional stdio CLI with the official MCP Python SDK v2."""

import argparse
import asyncio
import json
from pathlib import Path
import sys


async def run(mode):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    scenario = Path(__file__).with_name("flaky-search.json").resolve()
    server = StdioServerParameters(command=sys.executable, args=[
        "-m", "tool_mockbox", "mcp-serve", str(scenario), "--no-wait"
    ])
    async with Client(server, mode=mode, read_timeout_seconds=15) as client:
        print("Tools:", ", ".join(tool.name for tool in (await client.list_tools()).tools))
        for attempt in range(1, 4):
            response = await client.call_tool("search_notes", {"query": "release"})
            payload = json.loads(response.content[0].text)
            if response.is_error:
                if payload["error"]["code"] != "temporary":
                    raise RuntimeError("Unexpected scripted error")
                print(f"Attempt {attempt}: scripted {payload['error']['code']}")
                continue
            if payload != response.structured_content:
                raise RuntimeError("Text and structured responses disagree")
            print(f"Attempt {attempt}: success")
            print(json.dumps(response.structured_content, ensure_ascii=True, indent=2))
            break
        else:
            raise RuntimeError("Retry budget exhausted")
        print("Synthetic MCP fixtures only; no model or real upstream tool was called.")


def main():
    parser = argparse.ArgumentParser(description="Exercise tool-mockbox through actual MCP stdio")
    parser.add_argument("--mode", choices=("auto", "legacy"), default="auto")
    args = parser.parse_args()
    from tool_mockbox.mcp_adapter import MCPDependencyError, _require_sdk
    try:
        _require_sdk()
    except MCPDependencyError as error:
        print(error, file=sys.stderr)
        return 2
    asyncio.run(run(args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
