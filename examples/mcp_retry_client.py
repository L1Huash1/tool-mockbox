"""A bounded client retry policy against real local MCP stdio fixtures.

Only a well-formed tool error with code "temporary" is retried. The JSON
summary retains every attempted call, including errors preceding recovery.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys


async def run(scenario, mode, max_attempts):
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be an integer from 1 to 10")
    from mcp import Client, MCPError
    from mcp.client.stdio import StdioServerParameters
    from tool_mockbox.core import ScenarioError, _signature, parse_json, snapshot

    summary = {"ok": False, "mode": mode, "max_attempts": max_attempts, "attempts": 0, "events": []}

    def stop(reason, message, code=1):
        summary.update(stop_reason=reason, message=message)
        return summary, code

    parameters = StdioServerParameters(command=sys.executable, args=[
        "-m", "tool_mockbox", "mcp-serve", str(scenario.resolve()), "--no-wait"
    ])
    async with Client(parameters, mode=mode, read_timeout_seconds=15) as client:
        for attempt in range(1, max_attempts + 1):
            summary["attempts"] = attempt
            try:
                response = await client.call_tool("search_notes", {"query": "release"})
            except MCPError as error:
                data = error.data if isinstance(error.data, dict) else {}
                summary["events"].append({"attempt": attempt, "status": "protocol_error",
                                          "code": error.code, "fixture_code": data.get("fixture_code")})
                return stop("protocol_error", "MCP protocol errors are not retried.")
            try:
                payload = parse_json(response.content[0].text)
            except (IndexError, AttributeError, ValueError, TypeError):
                summary["events"].append({"attempt": attempt, "status": "invalid_response"})
                return stop("invalid_response", "The fixture response did not contain valid JSON text; no retry.")
            if response.is_error:
                error = payload.get("error") if isinstance(payload, dict) else None
                error_code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                if (not isinstance(error_code, str) or not error_code.strip()
                        or not isinstance(message, str) or not message.strip()):
                    summary["events"].append({"attempt": attempt, "status": "invalid_response"})
                    return stop("invalid_response", "The tool error needs nonblank code and message strings; no retry.")
                summary["events"].append({"attempt": attempt, "status": "tool_error", "code": error_code})
                if error_code != "temporary":
                    return stop("non_temporary_error", "Only tool errors with code temporary are retried.")
                if attempt == max_attempts:
                    return stop("max_attempts", f"Retry budget exhausted after {attempt} attempts; last error was temporary.")
                continue
            try:
                # Use the fixture engine's JSON equality: true differs from 1,
                # while object order and equivalent numeric spelling do not.
                consistent = (isinstance(payload, dict) and set(payload) == {"result"}
                              and _signature(payload) == _signature(snapshot(response.structured_content)))
            except ScenarioError:
                consistent = False
            if not consistent:
                summary["events"].append({"attempt": attempt, "status": "invalid_response"})
                return stop("invalid_response", "Success text and structured result wrappers did not agree; no retry.")
            summary["events"].append({"attempt": attempt, "status": "ok"})
            summary.update(ok=True, result=payload["result"])
            return stop("success", "The local synthetic fixture recovered within the retry budget.", 0)


def attempts_type(value):
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("max-attempts must be an integer from 1 to 10") from error
    if not 1 <= count <= 10:
        raise argparse.ArgumentTypeError("max-attempts must be an integer from 1 to 10")
    return count


def main():
    parser = argparse.ArgumentParser(description="Bounded MCP retries for temporary synthetic tool failures only")
    parser.add_argument("--scenario", type=Path, default=Path(__file__).with_name("flaky-search.json"))
    parser.add_argument("--mode", choices=("auto", "legacy"), default="auto")
    parser.add_argument("--max-attempts", type=attempts_type, default=3)
    args = parser.parse_args()
    from tool_mockbox.mcp_adapter import MCPDependencyError, _require_sdk
    try:
        _require_sdk()
    except MCPDependencyError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        summary, code = asyncio.run(run(args.scenario, args.mode, args.max_attempts))
    except Exception as error:
        print(f"MCP client setup failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
