import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from mcp import Client
except ImportError:
    HAS_MCP = False
else:
    HAS_MCP = True


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/mcp_retry_client.py"


def fixture(*steps, name="search_notes"):
    return {"schema_version": 1, "tools": [{"name": name, "steps": list(steps)}]}


def success(value):
    return {"arguments": {"query": "release"}, "status": "ok", "result": value}


def failure(code):
    return {"arguments": {"query": "release"}, "status": "error", "error": {"code": code, "message": "Synthetic error"}}


class RetryArgumentTests(unittest.TestCase):
    def test_attempt_limit_is_checked_before_optional_dependency(self):
        for count in ("0", "11", "-1", "1.5", "true"):
            with self.subTest(count=count):
                result = subprocess.run([sys.executable, str(EXAMPLE), "--max-attempts", count], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"integer from 1 to 10", result.stderr)
                self.assertEqual(result.stdout, b"")


@unittest.skipUnless(HAS_MCP, "optional MCP SDK v2 extra is not installed")
class RetryStdioTests(unittest.TestCase):
    def invoke(self, *options, data=None):
        with tempfile.TemporaryDirectory() as directory:
            arguments = [sys.executable, str(EXAMPLE), *options]
            if data is not None:
                path = Path(directory) / "synthetic.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                arguments.extend(["--scenario", str(path)])
            result = subprocess.run(arguments, capture_output=True, timeout=25)
        self.assertNotIn(b"Traceback", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_default_recovers_on_third_attempt_in_both_sdk_modes(self):
        for mode in ("auto", "legacy"):
            with self.subTest(mode=mode):
                code, summary = self.invoke("--mode", mode)
                self.assertEqual(code, 0)
                self.assertEqual(summary["attempts"], 3)
                self.assertEqual([event["status"] for event in summary["events"]], ["tool_error", "tool_error", "ok"])
                self.assertEqual([event.get("code") for event in summary["events"]], ["temporary", "temporary", None])
                self.assertEqual(summary["stop_reason"], "success")

    def test_two_attempt_budget_stops_without_calling_success_step(self):
        code, summary = self.invoke("--max-attempts", "2")
        self.assertEqual(code, 1)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["stop_reason"], "max_attempts")
        self.assertIn("exhausted after 2", summary["message"])
        self.assertEqual(len(summary["events"]), 2)

    def test_non_temporary_tool_error_is_not_retried(self):
        code, summary = self.invoke(data=fixture(failure("permission_denied"), success("must not call")))
        self.assertEqual(code, 1)
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["stop_reason"], "non_temporary_error")
        self.assertEqual(summary["events"][0]["code"], "permission_denied")

    def test_argument_mismatch_protocol_error_is_not_retried(self):
        step = success("must not call")
        step["arguments"] = {"query": "something else"}
        code, summary = self.invoke(data=fixture(step, success("must not call")))
        self.assertEqual(code, 1)
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["stop_reason"], "protocol_error")
        self.assertEqual(summary["events"][0]["fixture_code"], "arguments_mismatch")

    def test_exhaustion_after_temporary_error_is_not_retried(self):
        code, summary = self.invoke(data=fixture(failure("temporary")))
        self.assertEqual(code, 1)
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["stop_reason"], "protocol_error")
        self.assertEqual(summary["events"][1]["fixture_code"], "sequence_exhausted")

    def test_null_success_with_maximum_budget_returns_immediately(self):
        code, summary = self.invoke("--max-attempts", "10", data=fixture(success(None)))
        self.assertEqual(code, 0)
        self.assertEqual(summary["attempts"], 1)
        self.assertIsNone(summary["result"])


@unittest.skipUnless(HAS_MCP, "optional MCP SDK v2 extra is not installed")
class RetryResponseTests(unittest.IsolatedAsyncioTestCase):
    async def invoke_response(self, response):
        spec = importlib.util.spec_from_file_location("retry_example_under_test", EXAMPLE)
        example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(example)
        class FakeClient:
            calls = 0
            async def __aenter__(self):
                return self
            async def __aexit__(self, *errors):
                return False
            async def call_tool(self, *arguments):
                self.calls += 1
                return response
        client = FakeClient()
        with patch("mcp.Client", return_value=client):
            summary, code = await example.run(Path("synthetic.json"), "auto", 3)
        self.assertEqual(client.calls, 1)
        self.assertEqual(code, 1)
        self.assertEqual(summary["stop_reason"], "invalid_response")

    async def test_temporary_error_requires_nonblank_code_and_message(self):
        for error in ({"code": "temporary"}, {"code": "temporary", "message": 7},
                      {"code": "temporary", "message": " "}):
            with self.subTest(error=error):
                await self.invoke_response(SimpleNamespace(is_error=True, structured_content=None,
                    content=[SimpleNamespace(text=json.dumps({"error": error}))]))

    async def test_success_wrapper_comparison_distinguishes_boolean_and_number(self):
        await self.invoke_response(SimpleNamespace(is_error=False, structured_content={"result": 1},
            content=[SimpleNamespace(text='{"result":true}')]))


if __name__ == "__main__":
    unittest.main()
