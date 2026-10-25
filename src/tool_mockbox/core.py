"""Validate finite JSON fixtures and consume tool steps without executing code."""

from __future__ import annotations

import asyncio
import copy
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import threading
import time
from typing import Any


MAX_DOCUMENT_BYTES = 1_048_576
MAX_TOOLS = 100
MAX_STEPS = 10_000
MAX_DELAY_MS = 5_000
MAX_EVENTS = 20_000
MAX_EVENT_BYTES = 8_388_608


class ScenarioError(ValueError):
    """Invalid scenario, request, or unsupported JSON representation."""


class MockToolError(Exception):
    """A scripted fixture failure; it is not an error from a real tool."""

    def __init__(self, code: str, message: str, call_id: str | None = None):
        self.code, self.message, self.call_id = code, message, call_id
        super().__init__(f"{code}: {message}")


class UnexpectedCall(MockToolError):
    """Unknown tool, wrong arguments, exhausted sequence, or event limit."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScenarioError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def _float(token):
    try:
        value = float(token)
        exact = math.isfinite(value) and Decimal(token) == Decimal(str(value))
    except (ValueError, OverflowError, InvalidOperation):
        exact = False
    if not exact:
        raise ScenarioError("Decimal exceeds supported round-trip precision")
    return value


def _constant(token):
    raise ScenarioError(f"Non-standard JSON number {token}")


def parse_json(text: str) -> Any:
    try:
        value = json.loads(text.removeprefix("\ufeff"), object_pairs_hook=_object,
                           parse_float=_float, parse_constant=_constant)
        _validate_json(value)
        return value
    except (ValueError, RecursionError) as error:
        if isinstance(error, ScenarioError):
            raise
        raise ScenarioError(f"Invalid JSON: {error}") from error


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ScenarioError("JSON nesting exceeds 64 levels")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise ScenarioError("Unpaired Unicode surrogates are unsupported") from error
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ScenarioError("JSON numbers must be finite")
        if isinstance(value, int) and value.bit_length() > 1024:
            raise ScenarioError("Integer exceeds supported size")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _validate_json(key, depth + 1)
            _validate_json(item, depth + 1)
        return
    raise ScenarioError("Expected finite JSON values and string object keys")


def snapshot(value: Any) -> Any:
    try:
        _validate_json(value)
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, ScenarioError):
            raise
        raise ScenarioError("Expected finite JSON data") from error
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ScenarioError("JSON value exceeds 1 MiB")
    return json.loads(encoded)


def _signature(value):
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", Decimal(str(value)))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_signature(item) for item in value))
    return ("object", tuple(sorted((key, _signature(item)) for key, item in value.items())))


def _keys(value, required, optional, label):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ScenarioError(f"{label}: expected keys {sorted(required)} with optional {sorted(optional)}")


def validate_scenario(data: Any) -> dict:
    data = snapshot(data)
    _keys(data, {"schema_version", "tools"}, set(), "scenario")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ScenarioError("scenario: schema_version must be 1")
    if not isinstance(data["tools"], list) or not 1 <= len(data["tools"]) <= MAX_TOOLS:
        raise ScenarioError("scenario: expected 1 to 100 tools")
    names = set()
    count = 0
    for tool in data["tools"]:
        _keys(tool, {"name", "steps"}, {"description"}, "tool")
        name = tool["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or name in names:
            raise ScenarioError("tool: name must be unique, nonblank, and at most 128 characters")
        names.add(name)
        if "description" in tool and not isinstance(tool["description"], str):
            raise ScenarioError("tool: description must be a string")
        if not isinstance(tool["steps"], list) or not 1 <= len(tool["steps"]) <= 1_000:
            raise ScenarioError("tool: expected 1 to 1000 steps")
        count += len(tool["steps"])
        for step in tool["steps"]:
            if (not isinstance(step, dict) or not isinstance(step.get("status"), str)
                    or step["status"] not in {"ok", "error"}):
                raise ScenarioError("step: status must be ok or error")
            required = {"arguments", "status", "result" if step["status"] == "ok" else "error"}
            _keys(step, required, {"delay_ms"}, "step")
            delay = step.get("delay_ms", 0)
            if type(delay) is not int or not 0 <= delay <= MAX_DELAY_MS:
                raise ScenarioError("step: delay_ms must be an integer from 0 to 5000")
            if step["status"] == "error":
                _keys(step["error"], {"code", "message"}, set(), "step.error")
                if any(not isinstance(step["error"][key], str) or not step["error"][key].strip()
                       for key in ("code", "message")):
                    raise ScenarioError("step.error: code and message must be nonblank strings")
    if count > MAX_STEPS:
        raise ScenarioError("scenario: total steps exceed 10000")
    return data


def read_text(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ScenarioError("Input document exceeds 1 MiB")
    return data.decode("utf-8-sig")


def load_scenario(path: str | Path) -> dict:
    return validate_scenario(parse_json(read_text(path)))


def validate_request(data: Any) -> dict:
    data = snapshot(data)
    _keys(data, {"tool", "arguments"}, set(), "request")
    if not isinstance(data["tool"], str) or not data["tool"].strip() or len(data["tool"]) > 128:
        raise ScenarioError("request.tool: expected a nonblank string of at most 128 characters")
    return data


class MockToolbox:
    """Consume a finite scripted sequence for each tool.

    Arguments are matched as full JSON values: object order is irrelevant,
    arrays are ordered, booleans differ from numbers, and 1 equals 1.0.
    A valid matching call consumes its step before waiting. Unknown, mismatched
    and exhausted calls are logged as rejected without consuming a step.
    Results, arguments and public reports are independent JSON snapshots.
    """

    def __init__(self, scenario: dict, *, no_wait: bool = False):
        if type(no_wait) is not bool:
            raise ScenarioError("no_wait must be a boolean")
        data = validate_scenario(scenario)
        self._tools = {tool["name"]: tool for tool in data["tools"]}
        self._positions = {name: 0 for name in self._tools}
        self._events = []
        self._event_bytes = 0
        self._lock = threading.Lock()
        self._start_ns = time.monotonic_ns()
        self.no_wait = no_wait

    @classmethod
    def from_file(cls, path: str | Path, *, no_wait: bool = False):
        return cls(load_scenario(path), no_wait=no_wait)

    def _ms(self):
        return (time.monotonic_ns() - self._start_ns) / 1_000_000

    def _reserve(self, name, arguments):
        request = validate_request({"tool": name, "arguments": arguments})
        with self._lock:
            if len(self._events) >= MAX_EVENTS:
                raise UnexpectedCall("event_limit", "The 20000-call event limit was reached")
            tool = self._tools.get(name)
            next_step = (tool["steps"][self._positions[name]]
                         if tool and self._positions[name] < len(tool["steps"]) else None)
            # Reserve space for the complete eventual event before consuming a
            # step. Accounting is deliberately conservative and bounded.
            cost = len(json.dumps(request, ensure_ascii=True).encode("utf-8")) + 1024
            if next_step:
                field = "result" if next_step["status"] == "ok" else "error"
                cost += len(json.dumps(next_step[field], ensure_ascii=True).encode("utf-8"))
            if self._event_bytes + cost > MAX_EVENT_BYTES:
                raise UnexpectedCall("event_limit", "The 8 MiB event-log budget was reached")
            self._event_bytes += cost
            event = {"id": f"call-{len(self._events) + 1}", "tool": request["tool"],
                     "arguments": request["arguments"], "started_ms": self._ms(),
                     "status": "running", "step": None, "requested_delay_ms": 0}
            self._events.append(event)
            rejection = None
            if tool is None:
                rejection = ("unknown_tool", "The tool is not defined in this scenario")
            elif self._positions[name] >= len(tool["steps"]):
                rejection = ("sequence_exhausted", "No scripted steps remain for this tool")
            else:
                step = tool["steps"][self._positions[name]]
                if _signature(step["arguments"]) != _signature(request["arguments"]):
                    rejection = ("arguments_mismatch", "Arguments do not match the next scripted step")
            if rejection:
                event.update(status="rejected", error={"code": rejection[0], "message": rejection[1]},
                             finished_ms=self._ms(), elapsed_ms=0)
                raise UnexpectedCall(*rejection, event["id"])
            self._positions[name] += 1
            event["step"] = self._positions[name]
            event["requested_delay_ms"] = step.get("delay_ms", 0)
            return event, step

    def _finish(self, event, step, *, cancelled=False):
        with self._lock:
            event["finished_ms"] = self._ms()
            event["elapsed_ms"] = event["finished_ms"] - event["started_ms"]
            event["status"] = "cancelled" if cancelled else step["status"]
            if not cancelled:
                field = "result" if step["status"] == "ok" else "error"
                event[field] = snapshot(step[field])

    def _value(self, event, step):
        if step["status"] == "error":
            raise MockToolError(step["error"]["code"], step["error"]["message"], event["id"])
        return snapshot(step["result"])

    def call(self, tool: str, arguments: Any) -> Any:
        event, step = self._reserve(tool, arguments)
        try:
            if not self.no_wait and step.get("delay_ms", 0):
                time.sleep(step["delay_ms"] / 1_000)
        except BaseException:
            self._finish(event, step, cancelled=True)
            raise
        self._finish(event, step)
        return self._value(event, step)

    async def acall(self, tool: str, arguments: Any) -> Any:
        event, step = self._reserve(tool, arguments)
        try:
            delay = 0 if self.no_wait else step.get("delay_ms", 0) / 1_000
            await asyncio.sleep(delay)
        except BaseException:
            self._finish(event, step, cancelled=True)
            raise
        self._finish(event, step)
        return self._value(event, step)

    def catalog(self) -> dict:
        with self._lock:
            return {"schema_version": 1, "tools": [
                {"name": name, "description": tool.get("description", ""),
                 "steps": len(tool["steps"]), "consumed": self._positions[name],
                 "remaining": len(tool["steps"]) - self._positions[name]}
                for name, tool in self._tools.items()]}

    def report(self) -> dict:
        with self._lock:
            events = copy.deepcopy(self._events)
            counts = {status: sum(event["status"] == status for event in events)
                      for status in ("ok", "error", "rejected", "running", "cancelled")}
            return {"schema_version": 1, "wait_mode": "disabled" if self.no_wait else "real",
                    "summary": {"calls": len(events), **counts}, "events": events}
