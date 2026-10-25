from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tool_mockbox import MockToolbox
from tool_mockbox.cli import main
from tool_mockbox.report import render_report


PROJECT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scenario = self.root / "scenario.json"
        self.requests = self.root / "requests.jsonl"
        self.output = self.root / "output"
        self.scenario.write_text(json.dumps({"schema_version": 1, "tools": [{"name": "lookup", "steps": [
            {"arguments": None, "status": "ok", "result": "answer"}]}]}), encoding="utf-8")
        self.requests.write_text('{"tool":"lookup","arguments":null}\n', encoding="utf-8")

    def run_cli(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["run", str(self.scenario), str(self.requests), "--output", str(self.output), *options])
        return code, stdout.getvalue(), stderr.getvalue()

    def read_report(self):
        return json.loads((self.output / "run.json").read_text(encoding="utf-8"))

    def test_success_writes_json_and_self_contained_html(self):
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("1 ok", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(self.read_report()["summary"]["ok"], 1)
        html = (self.output / "report.html").read_text(encoding="utf-8")
        self.assertIn("answer", html)
        self.assertNotIn('src="http', html)

    def test_scripted_errors_and_unexpected_calls_return_one_and_continue(self):
        self.scenario.write_text(json.dumps({"schema_version": 1, "tools": [{"name": "lookup", "steps": [
            {"arguments": None, "status": "error", "error": {"code": "temporary", "message": "fake"}},
            {"arguments": None, "status": "ok", "result": "answer"}]}]}), encoding="utf-8")
        self.requests.write_text('{"tool":"unknown","arguments":null}\n' + '{"tool":"lookup","arguments":null}\n' * 2, encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 1)
        summary = self.read_report()["summary"]
        self.assertEqual((summary["calls"], summary["rejected"], summary["error"], summary["ok"]), (3, 1, 1, 1))

    def test_scripted_error_named_event_limit_does_not_stop_run(self):
        data = {"schema_version": 1, "tools": [{"name": "lookup", "steps": [
            {"arguments": None, "status": "error", "error": {"code": "event_limit", "message": "scripted"}},
            {"arguments": None, "status": "ok", "result": "continued"}]}]}
        self.scenario.write_text(json.dumps(data), encoding="utf-8")
        self.requests.write_text('{"tool":"lookup","arguments":null}\n' * 2, encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 1)
        report = self.read_report()
        self.assertEqual(report["summary"]["calls"], 2)
        self.assertEqual(report["summary"]["ok"], 1)
        self.assertIsNone(report["stopped_early"])

    def test_log_limit_returns_two_and_saves_explicit_partial_report(self):
        self.requests.write_text('{"tool":"lookup","arguments":null}\n' * 2, encoding="utf-8")
        with patch("tool_mockbox.core.MAX_EVENTS", 1):
            code, _, stderr = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("partial", stderr)
        report = self.read_report()
        self.assertEqual(report["request_count"], 2)
        self.assertEqual(report["summary"]["calls"], 1)
        self.assertEqual(report["stopped_early"]["code"], "event_limit")
        self.assertIn("Partial run", (self.output / "report.html").read_text(encoding="utf-8"))

    def test_existing_output_preserves_files_and_inputs(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("original", encoding="utf-8")
        original = self.scenario.read_bytes()
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.scenario.read_bytes(), original)
        self.assertFalse((self.output / "run.json").exists())

    def test_source_path_cannot_be_used_as_output(self):
        self.output = self.scenario
        before = self.scenario.read_bytes()
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.scenario.read_bytes(), before)

    def test_invalid_request_line_gives_line_number_without_creating_output(self):
        self.requests.write_text('\n{"tool":"lookup","arguments":null}\n{"bad":1}\n', encoding="utf-8")
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("Request line 3", stderr)
        self.assertFalse(self.output.exists())

    def test_blank_request_list_produces_valid_empty_report(self):
        self.requests.write_text("\n  \n", encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.read_report()["summary"]["calls"], 0)

    def test_no_wait_option_is_visible_in_report(self):
        self.assertEqual(self.run_cli("--no-wait")[0], 0)
        self.assertEqual(self.read_report()["wait_mode"], "disabled")

    def test_oversized_request_document_and_too_many_requests_fail(self):
        self.requests.write_bytes(b" " * 1_048_577)
        self.assertEqual(self.run_cli()[0], 2)
        self.requests.write_text('{"tool":"lookup","arguments":null}\n' * 10_001, encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 2)
        self.assertFalse(self.output.exists())

    def test_invalid_scenario_utf8_and_missing_input_fail_cleanly(self):
        for body in (b"\xff", b"{}"):
            self.scenario.write_bytes(body)
            code, _, stderr = self.run_cli()
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", stderr)
        self.scenario.unlink()
        self.assertEqual(self.run_cli()[0], 2)

    def test_module_entrypoint_version_help_and_bad_command(self):
        for options, expected in ((["--version"], 0), (["--help"], 0), (["unknown"], 2)):
            result = subprocess.run([sys.executable, "-m", "tool_mockbox", *options], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, expected)
        self.assertIn(b"0.1.0", subprocess.run([sys.executable, "-m", "tool_mockbox", "--version"], capture_output=True, timeout=10).stdout)

    def test_serve_keyboard_interrupt_closes_server_and_prints_bound_port(self):
        class FakeServer:
            server_port = 12345
            closed = False
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                self.closed = True
        server = FakeServer()
        stdout = io.StringIO()
        with patch("tool_mockbox.cli.create_server", return_value=server) as create, redirect_stdout(stdout):
            code = main(["serve", str(self.scenario), "--port", "0"])
        self.assertEqual(code, 130)
        self.assertTrue(server.closed)
        self.assertEqual(create.call_args.kwargs["port"], 0)
        self.assertIn("127.0.0.1:12345", stdout.getvalue())

    def test_examples_run_as_documented(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["run", str(PROJECT / "examples/healthy.json"), str(PROJECT / "examples/healthy-requests.jsonl"),
                         "--output", str(self.output)])
        self.assertEqual(code, 0)
        result = subprocess.run([sys.executable, str(PROJECT / "examples/retry_demo.py")], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Attempt 3: success", result.stdout)


class ReportTests(unittest.TestCase):
    def test_all_fixture_text_is_escaped_in_html(self):
        attack = '</script><img src=x onerror="alert(1)">'
        box = MockToolbox({"schema_version": 1, "tools": [{"name": attack, "steps": [
            {"arguments": attack, "status": "ok", "result": {attack: attack}}]}]})
        box.call(attack, attack)
        rendered = render_report(box.report())
        self.assertNotIn(attack, rendered)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;/script&gt;", rendered)
        self.assertEqual(rendered.count("<script>"), 1)
        self.assertIn('id="match-count" role="status" aria-live="polite"', rendered)
        self.assertIn('id="no-match"', rendered)

    def test_running_and_empty_reports_render_without_missing_fields(self):
        report = {"wait_mode": "real", "summary": {"calls": 1, "ok": 0, "error": 0, "rejected": 0, "cancelled": 0, "running": 1},
                  "events": [{"id": "call-1", "tool": "lookup", "arguments": None, "status": "running", "step": 1, "requested_delay_ms": 1}]}
        self.assertIn("pending", render_report(report))
        report["events"] = []
        report["summary"]["calls"] = 0
        self.assertIn("No calls were recorded", render_report(report))


if __name__ == "__main__":
    unittest.main()
