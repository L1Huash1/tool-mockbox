from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import PackageNotFoundError
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tool_mockbox import MockToolbox, ScenarioError
from tool_mockbox.cli import main
from tool_mockbox.mcp_adapter import MCPDependencyError, create_mcp_server, validate_mcp_fixture


def fixture(name="lookup", arguments=None):
    return {"schema_version": 1, "tools": [{"name": name, "steps": [
        {"arguments": {} if arguments is None else arguments, "status": "ok", "result": None}
    ]}]}


class MCPContractTests(unittest.TestCase):
    def test_names_must_keep_mcp_ascii_contract_without_changing_core(self):
        for name in ("带中文", "with space", "a/b", "a:b", "line\nname"):
            with self.subTest(name=name):
                box = MockToolbox(fixture(name))
                with self.assertRaisesRegex(ScenarioError, "ASCII"):
                    validate_mcp_fixture(box)
                self.assertIsNone(box.call(name, {}))
        for name in ("a", "A_2.query-v1", "x" * 128):
            validate_mcp_fixture(MockToolbox(fixture(name)))

    def test_every_step_must_have_object_arguments(self):
        for arguments in (None, [], 1, True, "text"):
            data = fixture()
            data["tools"][0]["steps"].append({"arguments": arguments, "status": "ok", "result": None})
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ScenarioError, "JSON object"):
                validate_mcp_fixture(MockToolbox(data))
        validate_mcp_fixture(MockToolbox(fixture(arguments={"a": [1, True, None]})))

    def test_invalid_fixture_fails_before_optional_dependency_import(self):
        with patch("tool_mockbox.mcp_adapter._require_sdk") as sdk:
            with self.assertRaises(ScenarioError):
                create_mcp_server(MockToolbox(fixture("bad/name")))
        sdk.assert_not_called()
        with self.assertRaises(TypeError):
            validate_mcp_fixture({})

    def test_missing_extra_gives_source_install_hint(self):
        with patch("tool_mockbox.mcp_adapter.version", side_effect=PackageNotFoundError):
            with self.assertRaises(MCPDependencyError) as caught:
                create_mcp_server(MockToolbox(fixture()))
        self.assertIn('python -m pip install ".[mcp]"', str(caught.exception))
        self.assertIn("cloned tool-mockbox source", str(caught.exception))

    def test_incompatible_sdk_version_gives_actionable_error(self):
        for installed in ("1.26.0", "2.2.9", "3.0.0", "invalid"):
            with self.subTest(installed=installed), patch("tool_mockbox.mcp_adapter.version", return_value=installed):
                with self.assertRaises(MCPDependencyError) as caught:
                    create_mcp_server(MockToolbox(fixture()))
                self.assertIn(installed, str(caught.exception))

    def test_cli_missing_extra_uses_stderr_and_leaves_stdout_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scenario.json"
            source.write_text(json.dumps(fixture()), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("tool_mockbox.mcp_adapter.version", side_effect=PackageNotFoundError), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["mcp-serve", str(source)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn('.[mcp]', stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_rejects_incompatible_fixture_without_protocol_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scenario.json"
            source.write_text(json.dumps(fixture("bad/name")), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["mcp-serve", str(source)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("ASCII", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
