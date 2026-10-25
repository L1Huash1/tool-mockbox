# tool-mockbox

[中文说明](README.zh-CN.md) · [Fixture HTTP protocol](docs/protocol.md) · [Optional MCP setup](docs/mcp.md)

**Test the code that calls agent tools with small, repeatable local fixtures.**

Describe a tool's next responses in JSON: two temporary failures, then a success. Your Python retry loop, async cancellation handling, or local HTTP client calls that fixture. Inspect the recorded calls in a self-contained HTML report.

No model, API key, or paid service is required. Default Python/HTTP modes have no third-party runtime dependencies. This is an early `0.2.0` developer utility; the new optional MCP adapter uses the official Python SDK v2. It does not run a real upstream tool, emulate a network outage, or evaluate an LLM.

**New in 0.2:** expose compatible fixtures to real MCP clients over stdio, with consistent structured responses and propagated cancellation. Install from source with `python -m pip install ".[mcp]"`, then try `python examples/mcp_client.py`. See [the setup, contract, and tested SDK versions](docs/mcp.md).

## Try it

Python **3.10+**. Clone the source; a package has not been published to PyPI.

```sh
git clone https://github.com/L1Huash1/tool-mockbox.git
cd tool-mockbox
python -m venv .venv
```

Use the virtual environment's Python and command, without changing your shell's activation settings:

**Windows PowerShell**

```powershell
.venv\Scripts\python -m pip install .
.venv\Scripts\tool-mockbox run examples/healthy.json examples/healthy-requests.jsonl --output run-output
.venv\Scripts\python examples/retry_demo.py
```

**macOS / Linux**

```sh
.venv/bin/python -m pip install .
.venv/bin/tool-mockbox run examples/healthy.json examples/healthy-requests.jsonl --output run-output
.venv/bin/python examples/retry_demo.py
```

Open `run-output/report.html` in a browser. The first command records one successful synthetic lookup. The retry example prints:

```text
Attempt 1: scripted temporary
Attempt 2: scripted temporary
Attempt 3: success — Review the release checklist.
This is scripted retry code, not an LLM agent or model benchmark.
```

Installation may download the build tool `setuptools`; fixture execution itself makes no external requests. The examples use synthetic data.

## A useful failure case

From an activated virtual environment (or using the full command paths above):

```sh
tool-mockbox run examples/flaky-search.json examples/retry-requests.jsonl --output retry-output
```

This records **two scripted errors and one success**, writes both reports, and exits **1**. That exit code describes the fixture outcomes; it does not mean the program crashed. `run` replays a request list; it does not decide when to retry. The separate Python example contains the actual retry loop.

```sh
tool-mockbox run examples/flaky-search.json examples/exhausted-requests.jsonl --output exhausted-output --no-wait
```

The fourth call is rejected with `sequence_exhausted`. `--no-wait` skips configured delays and labels the report accordingly; the resulting durations are not latency measurements for a real service.

## Fixture format

```json
{
  "schema_version": 1,
  "tools": [{
    "name": "search_notes",
    "description": "Synthetic response sequence",
    "steps": [
      {
        "arguments": {"query": "release"},
        "status": "error",
        "error": {"code": "temporary", "message": "Synthetic temporary failure"},
        "delay_ms": 15
      },
      {
        "arguments": {"query": "release"},
        "status": "ok",
        "result": {"notes": ["Review the checklist"]}
      }
    ]
  }]
}
```

Every nonblank line in the request file is a complete JSON object:

```json
{"tool":"search_notes","arguments":{"query":"release"}}
```

Unknown fields and duplicate JSON keys are rejected. Steps have either `result` for `ok`, or an `error` object with nonblank `code` and `message` for `error`. No fixture string is executed as a command or expression.

## Use from Python

```python
from tool_mockbox import MockToolbox, MockToolError, UnexpectedCall

box = MockToolbox.from_file("examples/flaky-search.json")
for attempt in range(3):
    try:
        result = box.call("search_notes", {"query": "release"})
    except UnexpectedCall:
        raise  # A wrong fixture call should not be treated as a transient failure.
    except MockToolError as error:
        if error.code != "temporary":
            raise
    else:
        print(result)
        break
else:
    raise RuntimeError("Retry budget exhausted")

report = box.report()
print(report["summary"])
```

Use `await box.acall(name, arguments)` from asyncio code (Trio is not supported). `box.catalog()` shows each tool's consumed and remaining steps. `box.report()` returns an independent JSON-compatible snapshot. To render a Python run:

```python
from pathlib import Path
from tool_mockbox.report import render_report

with Path("python-report.html").open("x", encoding="utf-8") as stream:
    stream.write(render_report(box.report()))
```

### Matching, consumption, and cancellation

- Each tool has its own finite sequence. A call must match **the whole JSON arguments value** for its next step. Object key order does not matter; array order does. `1` matches `1.0`; `true` does not match `1`. No wildcards or partial matching are provided.
- A matching call reserves and consumes its step **before** waiting. Scripted errors also consume a step. Unknown tools, mismatched arguments, and exhausted sequences are logged as `rejected` without consuming a step.
- Concurrent calls reserve under a lock. Reservation order determines response order; completion order can differ. Delays do not hold the lock.
- Async cancellation while waiting records `cancelled`, keeps the step consumed, and propagates `asyncio.CancelledError`. `--no-wait` async calls still yield once. A task cancelled before it starts creates no event. Interrupted synchronous waits propagate the original exception, too.
- Invalid JSON-compatible Python input creates no event. When the log limit is reached, the attempted call is neither logged nor consumed; `UnexpectedCall(code="event_limit")` is raised.

## Local HTTP mode

In one terminal:

```sh
tool-mockbox serve examples/healthy.json --port 8769
```

In another terminal, using the same virtual environment:

```sh
python examples/http_client.py --port 8769
```

The service binds only `127.0.0.1`. Port `0` asks the operating system for an available port and prints the actual address. It provides `GET /catalog`, `GET /events`, and `POST /call`; see the [protocol and status codes](docs/protocol.md). A new process starts fresh state. There is no reset endpoint or persistent store.

This small server is for trusted local development. It has no authentication, TLS, process isolation, or production concurrency protection. Host and Origin checks reject foreign browser origins, and CORS is disabled; any program on the same computer can still access it. Do not expose or forward its port to a public network.

## Reports, limits, and exit codes

`run` exclusively creates a **new** output directory containing `run.json` and `report.html`; it refuses an existing path. It never replaces the scenario or request file. Output is not transactional: interruption or an I/O error may leave a partial directory. Choose a new output path when rerunning.

The HTML report includes status/search filters, visible call counts, requested and observed durations, arguments, and outcomes. It has no external resources or uploads; all calls remain readable with JavaScript disabled. Arguments and results are recorded **without redaction**. Keep reports within their intended audience.

| Limit | Value |
| --- | --- |
| Scenario file or aggregate JSONL input | 1 MiB raw bytes each |
| JSON value snapshot, including the full scenario | 1 MiB of ASCII-escaped JSON |
| Tools / steps per tool / steps total | 100 / 1,000 / 10,000 |
| JSON nesting / integer size | 64 levels / 1,024 bits |
| Delay per step | 0–5,000 ms |
| Recorded events | 20,000 |
| Event-log accounting | 8 MiB, conservatively reserved before calls |
| HTTP call body | 65,536 bytes |
| HTTP socket inactivity timeout | 5 seconds |

The event budget accounts for compact ASCII-escaped payloads and overhead, not the final indented JSON/HTML file size. Reports may be larger than that accounting total. Excess input, unpaired Unicode surrogates, non-finite numbers, and decimal JSON numbers outside supported round-trip precision are rejected. Unicode takes more space in the conservative ASCII representation. JSONL request count is capped at 10,000.

| `run` exit code | Meaning |
| --- | --- |
| `0` | All recorded calls succeeded, including an empty request list |
| `1` | Scripted error, rejection, cancellation, or unfinished call exists |
| `2` | Invalid input, I/O/usage error, or a log limit stopped the run |

On log exhaustion, the CLI saves an explicitly marked partial report and stops. Scripted error codes may contain the text `event_limit`; only an actual fixture log-limit exception stops execution. `serve` runs until interrupted and returns `130` for a keyboard interruption.

## Development and scope

```sh
python -m pip install .
python -m unittest discover -s tests -v
```

CI runs the base tests and healthy/retry examples on Windows and Linux with Python 3.10 and 3.13. A separate four-job matrix installs MCP SDK 2.3.0 and exercises official-client auto/legacy modes and real stdio child processes. Without the extra, integration tests are skipped and the base/adapter contract tests still run. Tests cover real loopback HTTP requests, cancellation, concurrent reservations, capacity boundaries, and CLI behavior. Each successful base Actions job provides a `healthy-report-...` artifact containing only the synthetic example's JSON and HTML reports, retained for 7 days; download it from the run's Artifacts section and open `report.html` locally.

Use an established server mocking tool when you need rich HTTP routing, recording/proxying, or network fault simulation. For example, [WireMock](https://wiremock.org/docs/stateful-behaviour/) already supports stateful scenarios. This project focuses on a small Python tool-call fixture and readable call history. It does not establish model quality, security, or performance guarantees.

MIT license. Developed with AI assistance; the implementation, examples, and tests are available for review. Reports and configured delays are not evidence of real upstream reliability.
