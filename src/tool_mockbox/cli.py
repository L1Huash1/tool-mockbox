"""Run a request fixture or serve the private loopback fixture protocol."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .core import MockToolbox, MockToolError, ScenarioError, UnexpectedCall, parse_json, read_text, validate_request
from .report import render_report
from .server import create_server


def load_requests(path):
    requests = []
    for number, line in enumerate(read_text(path).split("\n"), 1):
        if line.strip():
            try:
                requests.append(validate_request(parse_json(line)))
            except ScenarioError as error:
                raise ScenarioError(f"Request line {number}: {error}") from error
    if len(requests) > 10_000:
        raise ScenarioError("Request list exceeds 10000 calls")
    return requests


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scripted local tool fixtures; no model or real tool execution.")
    parser.add_argument("--version", action="version", version=f"tool-mockbox {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a JSONL request list and save a new local report directory")
    run.add_argument("scenario", type=Path)
    run.add_argument("requests", type=Path)
    run.add_argument("--output", type=Path, required=True, help="new directory for run.json and report.html")
    run.add_argument("--no-wait", action="store_true", help="skip configured delays; does not simulate real latency")
    serve = sub.add_parser("serve", help="Serve the fixture protocol on 127.0.0.1 only")
    serve.add_argument("scenario", type=Path)
    serve.add_argument("--port", type=int, default=8769, help="port 0 selects a free port")
    serve.add_argument("--no-wait", action="store_true")
    args = parser.parse_args(argv)
    try:
        toolbox = MockToolbox.from_file(args.scenario, no_wait=args.no_wait)
        if args.command == "serve":
            server = create_server(toolbox, port=args.port)
            print(f"tool-mockbox listening at http://127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                return 130
            finally:
                server.server_close()
            return 0
        requests = load_requests(args.requests)
        # Exclusive directory creation prevents report output from overwriting
        # either source document or a previous run's files.
        args.output.mkdir(parents=True, exist_ok=False)
        stopped = None
        for request in requests:
            try:
                toolbox.call(request["tool"], request["arguments"])
            except MockToolError as error:
                if isinstance(error, UnexpectedCall) and error.code == "event_limit":
                    stopped = {"code": error.code, "message": error.message}
                    break
        report = toolbox.report()
        report["request_count"] = len(requests)
        report["stopped_early"] = stopped
        (args.output / "run.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        (args.output / "report.html").write_text(render_report(report), encoding="utf-8")
        summary = report["summary"]
        print(f"Recorded {summary['calls']} calls: {summary['ok']} ok, {summary['error']} scripted errors, {summary['rejected']} rejected.")
        print(f"Reports saved in {args.output}")
        if stopped:
            print("tool-mockbox: run stopped at the event-log limit; report is partial", file=sys.stderr)
            return 2
        return int(any(summary[key] for key in ("error", "rejected", "cancelled", "running")))
    except (ScenarioError, OSError, UnicodeError, ValueError, RecursionError) as error:
        print(f"tool-mockbox: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
