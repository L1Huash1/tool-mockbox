import asyncio
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tool_mockbox import MockToolbox
from tool_mockbox.mcp_adapter import create_mcp_server

try:
    from mcp import Client, MCPError
    from mcp.client.stdio import StdioServerParameters
    from mcp.types import INVALID_PARAMS
except ImportError:
    HAS_MCP = False
else:
    HAS_MCP = True


MODES = ("auto", "legacy")
PROJECT = Path(__file__).resolve().parents[1]


def fixture(*steps):
    return {"schema_version": 1, "tools": [{"name": "lookup", "description": "Synthetic lookup", "steps": list(steps)}]}


def ok(value, *, delay_ms=0):
    return {"arguments": {}, "status": "ok", "result": value, "delay_ms": delay_ms}


def error(code="temporary"):
    return {"arguments": {}, "status": "error", "error": {"code": code, "message": "Synthetic failure"}}


async def until(predicate, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Timed out waiting for observed fixture state")


@unittest.skipUnless(HAS_MCP, "optional MCP SDK v2 extra is not installed")
class MCPMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_declares_generic_object_schema_and_no_inferred_fields(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                box = MockToolbox(fixture(ok(1)))
                async with Client(create_mcp_server(box), mode=mode) as client:
                    listed = await client.list_tools()
                    self.assertEqual([tool.name for tool in listed.tools], ["lookup"])
                    self.assertEqual(listed.tools[0].input_schema, {"type": "object"})
                    self.assertEqual(listed.tools[0].description, "Synthetic lookup")
                    self.assertEqual(box.report()["summary"]["calls"], 0)

    async def test_success_text_and_structured_content_share_wrapper_for_all_result_types(self):
        values = [None, True, 3, 1.25, "你好", [1, "a"], {"nested": {"x": 1}}]
        for mode in MODES:
            with self.subTest(mode=mode):
                box = MockToolbox(fixture(*(ok(value) for value in values)))
                async with Client(create_mcp_server(box), mode=mode) as client:
                    for value in values:
                        result = await client.call_tool("lookup", {})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content, {"result": value})
                        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                self.assertEqual(box.report()["summary"]["ok"], len(values))

    async def test_scripted_failures_are_tool_errors_even_with_reserved_error_code(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                box = MockToolbox(fixture(error(), error("event_limit"), ok("continued")))
                async with Client(create_mcp_server(box), mode=mode) as client:
                    for index, code in enumerate(("temporary", "event_limit"), 1):
                        result = await client.call_tool("lookup", {})
                        self.assertTrue(result.is_error)
                        self.assertIsNone(result.structured_content)
                        payload = json.loads(result.content[0].text)
                        self.assertEqual(payload["error"]["code"], code)
                        self.assertEqual(payload["call_id"], f"call-{index}")
                    self.assertEqual((await client.call_tool("lookup", {})).structured_content, {"result": "continued"})

    async def test_fixture_contract_rejections_are_protocol_errors_and_keep_state(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                box = MockToolbox(fixture(ok("value")))
                async with Client(create_mcp_server(box), mode=mode) as client:
                    for name, arguments, code in (("missing", {}, "unknown_tool"), ("lookup", {"wrong": 1}, "arguments_mismatch")):
                        with self.assertRaises(MCPError) as caught:
                            await client.call_tool(name, arguments)
                        self.assertEqual(caught.exception.code, INVALID_PARAMS)
                        self.assertEqual(caught.exception.data["fixture_code"], code)
                        self.assertTrue(caught.exception.data["call_id"].startswith("call-"))
                    self.assertEqual(box.catalog()["tools"][0]["remaining"], 1)
                    await client.call_tool("lookup", {})
                    with self.assertRaises(MCPError) as caught:
                        await client.call_tool("lookup", {})
                    self.assertEqual(caught.exception.data["fixture_code"], "sequence_exhausted")
                self.assertEqual(box.report()["summary"]["rejected"], 3)

    async def test_omitted_arguments_match_empty_object(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                box = MockToolbox(fixture(ok("empty")))
                async with Client(create_mcp_server(box), mode=mode) as client:
                    result = await client.call_tool("lookup")
                    self.assertEqual(result.structured_content, {"result": "empty"})

    async def test_cancel_reaches_fixture_and_next_step_stays_available(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                box = MockToolbox(fixture(ok("waiting", delay_ms=5000), ok("after cancellation")))
                async with Client(create_mcp_server(box), mode=mode, read_timeout_seconds=10) as client:
                    task = asyncio.create_task(client.call_tool("lookup", {}))
                    await until(lambda: box.report()["summary"]["running"] == 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await until(lambda: box.report()["summary"]["cancelled"] == 1)
                    result = await client.call_tool("lookup", {})
                    self.assertEqual(result.structured_content, {"result": "after cancellation"})
                self.assertEqual(box.report()["summary"]["running"], 0)
                self.assertEqual(box.catalog()["tools"][0]["consumed"], 2)

    async def test_log_limit_is_protocol_error_without_event_or_consumption(self):
        box = MockToolbox(fixture(ok("value")))
        async with Client(create_mcp_server(box)) as client:
            with patch("tool_mockbox.core.MAX_EVENTS", 0):
                with self.assertRaises(MCPError) as caught:
                    await client.call_tool("lookup", {})
            self.assertEqual(caught.exception.code, INVALID_PARAMS)
            self.assertEqual(caught.exception.data, {"fixture_code": "event_limit", "call_id": None})
            self.assertEqual(box.report()["events"], [])
            self.assertEqual((await client.call_tool("lookup", {})).structured_content, {"result": "value"})


@unittest.skipUnless(HAS_MCP, "optional MCP SDK v2 extra is not installed")
class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_module_cli_over_stdio_supports_both_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                scenario = Path(directory) / "fixture.json"
                scenario.write_text(json.dumps(fixture(error(), ok({"answer": "local"}))), encoding="utf-8")
                params = StdioServerParameters(command=sys.executable, args=["-m", "tool_mockbox", "mcp-serve", str(scenario)])
                async with Client(params, mode=mode, read_timeout_seconds=15) as client:
                    self.assertEqual((await client.list_tools()).tools[0].name, "lookup")
                    self.assertTrue((await client.call_tool("lookup", {})).is_error)
                    success = await client.call_tool("lookup", {})
                    self.assertEqual(success.structured_content, {"result": {"answer": "local"}})
                    self.assertEqual(json.loads(success.content[0].text), success.structured_content)
                    with self.assertRaises(MCPError) as caught:
                        await client.call_tool("lookup", {})
                    self.assertEqual(caught.exception.data["fixture_code"], "sequence_exhausted")

    async def test_actual_stdio_cancellation_is_observed_before_continued_call(self):
        for mode in MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "fixture.json"
                state = Path(directory) / "state.json"
                source.write_text(json.dumps(fixture(ok("wait", delay_ms=5000), ok("after"))), encoding="utf-8")
                observer = Path(__file__).with_name("stdio_observer.py")
                params = StdioServerParameters(command=sys.executable, args=[str(observer), str(source), str(state)])
                def count(status):
                    try:
                        return json.loads(state.read_text(encoding="utf-8"))["summary"][status]
                    except (FileNotFoundError, json.JSONDecodeError):
                        return 0
                async with Client(params, mode=mode, read_timeout_seconds=15) as client:
                    task = asyncio.create_task(client.call_tool("lookup", {}))
                    await until(lambda: count("running") == 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await until(lambda: count("cancelled") == 1)
                    result = await client.call_tool("lookup", {})
                    self.assertEqual(result.structured_content, {"result": "after"})
                    await until(lambda: count("ok") == 1)


@unittest.skipUnless(HAS_MCP, "optional MCP SDK v2 extra is not installed")
class MCPWireTests(unittest.TestCase):
    def test_stdout_contains_only_protocol_json_and_nonobject_arguments_do_not_consume(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario = Path(directory) / "fixture.json"
            scenario.write_text(json.dumps(fixture(ok("preserved"))), encoding="utf-8")
            process = subprocess.Popen([sys.executable, "-m", "tool_mockbox", "mcp-serve", str(scenario)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", bufsize=1)
            lines, captured = queue.Queue(), []
            def read():
                for line in process.stdout:
                    captured.append(line)
                    lines.put(line)
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            def send(value):
                process.stdin.write(json.dumps(value) + "\n")
                process.stdin.flush()
            def response(request_id):
                while True:
                    value = json.loads(lines.get(timeout=15))
                    self.assertEqual(value["jsonrpc"], "2.0")
                    if value.get("id") == request_id:
                        return value
            try:
                send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "mockbox-wire-test", "version": "1"}}})
                self.assertEqual(response(1)["result"]["protocolVersion"], "2025-11-25")
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lookup", "arguments": []}})
                self.assertIn("error", response(2))
                send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lookup", "arguments": {}}})
                result = response(3)["result"]
                self.assertEqual(result["structuredContent"], {"result": "preserved"})
                self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
                process.stdin.close()
                self.assertEqual(process.wait(timeout=15), 0)
                reader.join(timeout=3)
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(captured), 3)
                for line in captured:
                    self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if not process.stdin.closed:
                    process.stdin.close()
                process.stdout.close()
                process.stderr.close()
                reader.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
