import asyncio
import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tool_mockbox import MockToolbox, MockToolError, ScenarioError, UnexpectedCall, load_scenario
from tool_mockbox.core import parse_json, snapshot, validate_scenario


def scenario(*steps, name="lookup"):
    return {"schema_version": 1, "tools": [{"name": name, "steps": list(steps)}]}


def ok(arguments=None, result=None, **fields):
    return {"arguments": arguments, "status": "ok", "result": result, **fields}


def failure(arguments=None, code="temporary", **fields):
    return {"arguments": arguments, "status": "error",
            "error": {"code": code, "message": "Synthetic failure"}, **fields}


class CoreTests(unittest.TestCase):
    def test_retry_sequence_is_consumed_and_scripted_errors_keep_call_ids(self):
        box = MockToolbox(scenario(failure({"q": "a"}), failure({"q": "a"}), ok({"q": "a"}, ["answer"])))
        for index in range(1, 3):
            with self.assertRaises(MockToolError) as caught:
                box.call("lookup", {"q": "a"})
            self.assertNotIsInstance(caught.exception, UnexpectedCall)
            self.assertEqual(caught.exception.call_id, f"call-{index}")
        self.assertEqual(box.call("lookup", {"q": "a"}), ["answer"])
        self.assertEqual(box.report()["summary"], {"calls": 3, "ok": 1, "error": 2, "rejected": 0, "running": 0, "cancelled": 0})

    def test_mismatch_does_not_consume_step(self):
        box = MockToolbox(scenario(ok({"id": 1}, "yes")))
        with self.assertRaises(UnexpectedCall) as caught:
            box.call("lookup", {"id": 2})
        self.assertEqual(caught.exception.code, "arguments_mismatch")
        self.assertEqual(box.catalog()["tools"][0]["consumed"], 0)
        self.assertEqual(box.call("lookup", {"id": 1}), "yes")

    def test_unknown_and_exhausted_calls_have_distinct_codes(self):
        box = MockToolbox(scenario(ok()))
        with self.assertRaises(UnexpectedCall) as unknown:
            box.call("missing", None)
        self.assertEqual(unknown.exception.code, "unknown_tool")
        box.call("lookup", None)
        with self.assertRaises(UnexpectedCall) as exhausted:
            box.call("lookup", None)
        self.assertEqual(exhausted.exception.code, "sequence_exhausted")
        self.assertEqual([event["status"] for event in box.report()["events"]], ["rejected", "ok", "rejected"])

    def test_json_matching_ignores_object_order_and_numeric_spelling(self):
        box = MockToolbox(scenario(ok({"a": 1, "b": {"x": 2}}, "ok")))
        self.assertEqual(box.call("lookup", {"b": {"x": 2.0}, "a": 1.0}), "ok")

    def test_booleans_and_array_order_do_not_match_numbers_or_sets(self):
        for wrong in ({"a": True}, {"a": [2, 1]}):
            expected = {"a": 1} if isinstance(wrong["a"], bool) else {"a": [1, 2]}
            with self.subTest(wrong=wrong):
                box = MockToolbox(scenario(ok(expected)))
                with self.assertRaises(UnexpectedCall):
                    box.call("lookup", wrong)
                self.assertEqual(box.catalog()["tools"][0]["remaining"], 1)

    def test_multiple_tools_keep_independent_positions(self):
        data = scenario(ok(1, "a"), ok(2, "b"))
        data["tools"].append({"name": "other", "steps": [ok(None, "c")]})
        box = MockToolbox(data)
        self.assertEqual(box.call("other", None), "c")
        self.assertEqual(box.call("lookup", 1), "a")
        self.assertEqual([tool["remaining"] for tool in box.catalog()["tools"]], [1, 0])

    def test_input_arguments_results_and_reports_are_independent_snapshots(self):
        data = scenario(ok({"items": [1]}, {"items": [2]}))
        box = MockToolbox(data)
        data["tools"][0]["steps"][0]["result"]["items"].append(999)
        arguments = {"items": [1]}
        result = box.call("lookup", arguments)
        arguments["items"].append(3)
        result["items"].append(4)
        report = box.report()
        report["events"][0]["result"]["items"].append(5)
        self.assertEqual(box.report()["events"][0]["arguments"], {"items": [1]})
        self.assertEqual(box.report()["events"][0]["result"], {"items": [2]})

    def test_configured_delay_waits_and_records_requested_duration(self):
        box = MockToolbox(scenario(ok(delay_ms=17)))
        with patch("tool_mockbox.core.time.sleep") as sleep:
            box.call("lookup", None)
        sleep.assert_called_once_with(0.017)
        event = box.report()["events"][0]
        self.assertEqual(event["requested_delay_ms"], 17)
        self.assertGreaterEqual(event["elapsed_ms"], 0)
        self.assertEqual(box.report()["wait_mode"], "real")

    def test_no_wait_skips_sleep_but_preserves_requested_delay(self):
        box = MockToolbox(scenario(ok(delay_ms=5000)), no_wait=True)
        with patch("tool_mockbox.core.time.sleep") as sleep:
            box.call("lookup", None)
        sleep.assert_not_called()
        self.assertEqual(box.report()["events"][0]["requested_delay_ms"], 5000)
        self.assertEqual(box.report()["wait_mode"], "disabled")

    def test_interruption_propagates_and_consumes_reserved_step(self):
        box = MockToolbox(scenario(ok(delay_ms=1), ok(result="next")))
        with patch("tool_mockbox.core.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                box.call("lookup", None)
        self.assertEqual(box.report()["events"][0]["status"], "cancelled")
        self.assertEqual(box.call("lookup", None), "next")

    def test_running_report_and_catalog_during_in_flight_call(self):
        entered, release = threading.Event(), threading.Event()
        box = MockToolbox(scenario(ok(result="done", delay_ms=1)))
        def wait(_):
            entered.set()
            self.assertTrue(release.wait(5))
        with patch("tool_mockbox.core.time.sleep", side_effect=wait), concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(box.call, "lookup", None)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(box.catalog()["tools"][0]["consumed"], 1)
                report = box.report()
                self.assertEqual(report["summary"]["running"], 1)
                self.assertNotIn("finished_ms", report["events"][0])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5), "done")

    def test_concurrent_reservations_never_reuse_a_step(self):
        count = 40
        box = MockToolbox(scenario(*(ok(None, value) for value in range(count))), no_wait=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: box.call("lookup", None), range(count)))
        self.assertEqual(sorted(results), list(range(count)))
        events = box.report()["events"]
        self.assertEqual([event["id"] for event in events], [f"call-{value}" for value in range(1, count + 1)])
        self.assertEqual([event["step"] for event in events], list(range(1, count + 1)))

    def test_aggregate_report_can_exceed_one_document_limit(self):
        # Each request is under 1 MiB; the cumulative log has its own budget.
        value = "x" * 600_000
        box = MockToolbox(scenario(ok()))
        for _ in range(2):
            with self.assertRaises(UnexpectedCall):
                box.call("unknown", value)
        report = box.report()
        self.assertEqual(report["summary"]["calls"], 2)
        self.assertGreater(len(json.dumps(report)), 1_048_576)

    def test_event_count_limit_does_not_consume_or_append(self):
        box = MockToolbox(scenario(ok(None, 1), ok(None, 2)))
        with patch("tool_mockbox.core.MAX_EVENTS", 1):
            self.assertEqual(box.call("lookup", None), 1)
            with self.assertRaises(UnexpectedCall) as caught:
                box.call("lookup", None)
            self.assertEqual(caught.exception.code, "event_limit")
            self.assertIsNone(caught.exception.call_id)
        self.assertEqual(box.report()["summary"]["calls"], 1)
        self.assertEqual(box.call("lookup", None), 2)

    def test_log_byte_limit_reserves_complete_result_before_consuming(self):
        box = MockToolbox(scenario(ok(None, "x" * 2000)))
        with patch("tool_mockbox.core.MAX_EVENT_BYTES", 1500):
            with self.assertRaises(UnexpectedCall) as caught:
                box.call("lookup", None)
        self.assertEqual(caught.exception.code, "event_limit")
        self.assertEqual(box.report()["summary"]["calls"], 0)
        self.assertEqual(box.catalog()["tools"][0]["remaining"], 1)
        self.assertEqual(len(box.call("lookup", None)), 2000)

    def test_invalid_request_creates_no_event(self):
        box = MockToolbox(scenario(ok()))
        for name, arguments in (("", None), (None, None), ("lookup", {1: "wrong"}), ("lookup", float("nan"))):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ScenarioError):
                box.call(name, arguments)
        self.assertEqual(box.report()["events"], [])

    def test_duplicate_keys_nonfinite_and_lossy_numbers_are_rejected(self):
        for text in ('{"x":1,"x":2}', 'NaN', 'Infinity', '-Infinity', '1e400', '0.1000000000000000001', '1e-400'):
            with self.subTest(text=text), self.assertRaises(ScenarioError):
                parse_json(text)
        self.assertEqual(parse_json('1.25e2'), 125.0)

    def test_unicode_bom_and_unicode_values_are_supported(self):
        self.assertEqual(parse_json('\ufeff{"消息":"你好"}'), {"消息": "你好"})
        box = MockToolbox(scenario(ok("你好", "成功"), name="查询"))
        self.assertEqual(box.call("查询", "你好"), "成功")

    def test_unpaired_surrogates_and_oversized_integers_are_rejected(self):
        for value in ("\ud800", 1 << 1024):
            with self.subTest(type=type(value).__name__), self.assertRaises(ScenarioError):
                snapshot(value)
        with self.assertRaises(ScenarioError):
            parse_json('"\\udfff"')

    def test_depth_cycles_and_non_json_python_objects_are_rejected(self):
        deep = None
        for _ in range(66):
            deep = [deep]
        cyclic = []
        cyclic.append(cyclic)
        for value in (deep, cyclic, (1, 2), {"a": {1, 2}}, {1: "a"}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ScenarioError):
                snapshot(value)

    def test_document_limit_is_conservative_ascii_json_size(self):
        with self.assertRaisesRegex(ScenarioError, "1 MiB"):
            snapshot("x" * 1_048_576)
        # Non-ASCII characters are counted as JSON escapes, too.
        with self.assertRaisesRegex(ScenarioError, "1 MiB"):
            snapshot("中" * 180_000)

    def test_scenario_rejects_unknown_fields_wrong_versions_and_duplicates(self):
        base = scenario(ok())
        cases = []
        for version in (True, 1.0, 2, "1"):
            cases.append({**base, "schema_version": version})
        cases.extend([{**base, "extra": 1}, {"schema_version": 1, "tools": []},
                      {"schema_version": 1, "tools": base["tools"] * 2}])
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ScenarioError):
                validate_scenario(data)

    def test_steps_reject_mixed_outcomes_and_invalid_delays(self):
        steps = [ok(error={"code": "x", "message": "x"}),
                 {"arguments": None, "status": "error", "error": {"code": " ", "message": "x"}},
                 {"arguments": None, "status": "unknown", "result": None}]
        steps.extend(ok(delay_ms=value) for value in (-1, 5001, True, 1.0))
        for step in steps:
            with self.subTest(step=step), self.assertRaises(ScenarioError):
                validate_scenario(scenario(step))

    def test_tool_step_and_total_step_limits(self):
        for data in (scenario(), scenario(*(ok() for _ in range(1001))),
                     {"schema_version": 1, "tools": [{"name": str(i), "steps": [ok()]} for i in range(101)]},
                     {"schema_version": 1, "tools": [{"name": str(i), "steps": [ok()] * 1000} for i in range(11)]}):
            with self.subTest(tools=len(data["tools"])), self.assertRaises(ScenarioError):
                validate_scenario(data)

    def test_no_wait_requires_a_boolean(self):
        for value in (0, 1, "yes", None):
            with self.subTest(value=value), self.assertRaises(ScenarioError):
                MockToolbox(scenario(ok()), no_wait=value)

    def test_file_loading_checks_size_utf8_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            path.write_bytes(b'\xef\xbb\xbf' + json.dumps(scenario(ok())).encode())
            self.assertEqual(load_scenario(path)["schema_version"], 1)
            for body, error in ((b"x" * 1_048_577, ScenarioError), (b"\xff", UnicodeError),
                                (b'{"schema_version":1,"schema_version":1,"tools":[]}', ScenarioError)):
                path.write_bytes(body)
                with self.subTest(body_size=len(body)), self.assertRaises(error):
                    load_scenario(path)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_success_and_scripted_error(self):
        box = MockToolbox(scenario(ok(None, "value"), failure()), no_wait=True)
        self.assertEqual(await box.acall("lookup", None), "value")
        with self.assertRaises(MockToolError):
            await box.acall("lookup", None)
        self.assertEqual(box.report()["summary"]["error"], 1)

    async def test_async_cancel_is_recorded_and_step_is_consumed(self):
        box = MockToolbox(scenario(ok(delay_ms=5000), ok(result="next")))
        task = asyncio.create_task(box.acall("lookup", None))
        await asyncio.sleep(0)
        self.assertEqual(box.report()["summary"]["running"], 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(box.report()["summary"]["cancelled"], 1)
        self.assertEqual(await box.acall("lookup", None), "next")

    async def test_no_wait_still_yields_so_cancellation_is_observable(self):
        box = MockToolbox(scenario(ok(delay_ms=5000)), no_wait=True)
        task = asyncio.create_task(box.acall("lookup", None))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(box.report()["summary"]["cancelled"], 1)

    async def test_concurrent_async_calls_reserve_in_creation_order(self):
        box = MockToolbox(scenario(*(ok(None, index) for index in range(12))), no_wait=True)
        self.assertEqual(await asyncio.gather(*(box.acall("lookup", None) for _ in range(12))), list(range(12)))
        self.assertEqual(box.catalog()["tools"][0]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
