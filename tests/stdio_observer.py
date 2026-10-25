"""Test-only CLI wrapper exposing synthetic core state through a temp file.

The actual adapter and stdio transport remain unchanged. This lets the test
wait until a request is reserved/cancelled, instead of guessing sleep times.
Nothing is printed to stdout.
"""

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import sys

from tool_mockbox import cli, mcp_adapter


def main():
    scenario, state = sys.argv[1:]
    original = mcp_adapter.serve_stdio

    async def observed_stdio(box):
        async def observe():
            while True:
                Path(state).write_text(json.dumps(box.report()), encoding="utf-8")
                await asyncio.sleep(0.005)
        watcher = asyncio.create_task(observe())
        try:
            await original(box)
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    mcp_adapter.serve_stdio = observed_stdio
    return cli.main(["mcp-serve", scenario])


if __name__ == "__main__":
    raise SystemExit(main())
