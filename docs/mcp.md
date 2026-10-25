# Optional MCP fixtures / 可选 MCP 接入

Version 0.2 adds an optional stdio adapter using the official MCP Python SDK v2. It exposes the same scripted local toolbox to an MCP client. Default Python and project-specific HTTP modes retain zero third-party runtime dependencies.

0.2 版增加了可选 MCP 接入。MCP 是客户端调用工具的一种标准协议；这里让已有的虚构工具通过该协议被调用，方便测试客户端代码。它不会连接真实搜索服务，也不需要模型或付费 API。

## Install from source / 从源码安装

From the cloned project directory, in your virtual environment:

```sh
python -m pip install ".[mcp]"
python examples/mcp_client.py
python examples/mcp_client.py --mode legacy
```

On Windows without activation, use `.venv\Scripts\python` instead of `python`; on macOS/Linux use `.venv/bin/python`. This extra installs `mcp>=2.3,<3` and its dependencies. The project has not been published to PyPI. Run these commands in the cloned source directory; `pip install tool-mockbox[mcp]` is not an advertised installation path.

请在项目源码目录内安装可选依赖。Windows 未激活环境时，将 `python` 换成 `.venv\Scripts\python`；macOS/Linux 换成 `.venv/bin/python`。默认安装不包含 SDK。依赖缺失或版本不兼容时，MCP 模式会向标准错误输出安装提示并退出，默认 Python / HTTP 模式仍可使用。

The example starts a real child process running the project's `mcp-serve` command, lists tools, and performs three calls. The first two return scripted errors; the third succeeds. It checks the agreement between text and structured results. It uses the official client, not a manually approximated protocol.

示例会启动真实的本地子进程，通过 MCP 列出工具并调用三次：两次预设失败后成功。客户端打印示例结果；服务器的标准输出只用于协议消息，不会打印启动横幅。

## Configure a stdio client / 配置客户端

Use your MCP client's documented stdio server settings. The equivalent executable and arguments are:

```text
command: ABSOLUTE_PATH_TO_VENV_PYTHON
args:
  - -m
  - tool_mockbox
  - mcp-serve
  - ABSOLUTE_PATH_TO_SCENARIO_JSON
```

Replace both paths with real absolute paths on your computer. Optional `--no-wait` skips configured delays; async calls still yield once. The client owns the process lifetime. Each process starts fresh scenario state. No network listener is created by `mcp-serve`.

请按所用客户端的配置格式填入解释器与场景文件的真实绝对路径。`--no-wait` 可选；开启后不应把耗时当作真实延迟。每个服务进程使用独立的新状态，客户端负责启动和关闭进程。该命令通过标准输入输出通信，不监听网络端口。

## Fixture contract / 场景要求

- Tool names must contain 1–128 ASCII letters, digits, `_`, `.`, or `-`.
- Every step's `arguments` must be a JSON **object**. Scalar, array, or null arguments are rejected when constructing the adapter. An omitted MCP arguments field is treated as `{}`.
- The adapter refuses incompatible scenarios; it never silently wraps or renames them. The core Python and local HTTP APIs continue to support their original broader JSON values and names.
- Every advertised tool uses the honest generic input schema `{"type":"object"}`. It does not infer field types or required keys. Exact argument matching is performed by the fixture engine against the next step.
- Only tool listing and calling are exposed. There are no prompts, resources, fixture reset tools, report tools, proxying, or real tool execution.

工具名限定为上述 ASCII 字符，每一步的参数都必须是对象。与 MCP 不兼容的场景会明确报错，不会悄悄改名或包装参数。工具描述中的输入格式仅表示「对象」，不代表已推断出业务参数结构；真正的匹配仍要求与下一步参数完全一致。

### Responses / 返回值

For success, both the JSON text block and `structured_content` contain the same wrapper:

```json
{"result":{"notes":["Review the release checklist."]}}
```

Any fixture result JSON can appear inside `result`, including null, arrays, and scalar values. For a scripted failure, the call returns `is_error: true`, a text block containing the following JSON, and no structured result:

```json
{"error":{"code":"temporary","message":"Synthetic temporary failure."},"call_id":"call-1"}
```

成功时，文本与结构化结果使用相同的 `{"result": ...}` 包装。预设失败属于工具级错误，客户端应检查 `is_error`，再按自己的策略处理；该失败仍然消耗一步。

Unknown tools, mismatched arguments, exhausted sequences, and log limits are protocol errors with `INVALID_PARAMS` (`-32602`). Their error data preserves `fixture_code` and `call_id`; for example:

```json
{"fixture_code":"arguments_mismatch","call_id":"call-2"}
```

These do not consume the next step. Unknown/mismatched/exhausted calls are recorded as rejected; a log-limit rejection cannot itself be recorded and has a null call ID. Invalid protocol/request data may be rejected by the SDK before the fixture sees it. A scenario's scripted error code named `event_limit` remains a tool error and is distinct from the engine's log-limit protocol error.

未知工具、参数不匹配、序列用尽和日志达到上限属于协议错误，保留具体原因 `fixture_code`。它们不消耗下一步。SDK 在调用进入模拟器前就拒绝的协议输入，不会形成模拟器记录。

### Cancellation and reports / 取消与报告

A matching call reserves its step before waiting. Client cancellation propagates into `toolbox.acall`, records a cancelled event, and keeps that step consumed. Later calls can use the next step. Cancellation before reservation creates no fixture event. Existing concurrency and log limits still apply.

The stdio command does **not** automatically save a JSON/HTML report. The client example prints the received responses. For programmatic access to call history, keep your `MockToolbox` instance and use its report API:

```python
from tool_mockbox import MockToolbox
from tool_mockbox.mcp_adapter import create_mcp_server

box = MockToolbox.from_file("examples/flaky-search.json")
server = create_mcp_server(box)
# Connect an official MCP Client to server, then inspect box.report().
```

取消会保留已经消耗的状态并继续抛出取消异常，下一次调用可使用后续响应。命令行 stdio 模式当前不会自动落盘报告；需要调用历史时，可以在 Python 中持有 `box` 并读取 `box.report()`。它不通过 MCP 暴露额外的调试工具。

## Compatibility and verification / 兼容范围

The integration matrix installs **MCP Python SDK 2.3.0** on Windows/Linux with Python 3.10/3.13. It exercises the official client's `auto` and `legacy` modes in memory and through real stdio subprocesses, including cancellation observed in the fixture before a continued call. `auto` currently negotiates protocol 2026-07-28; `legacy` uses the SDK's 2025-11-25 protocol path. The dependency range permits later SDK 2.x releases; this matrix does not claim to have tested every allowed release or every MCP client.

Async execution currently requires **asyncio**, because the fixture engine uses `asyncio.sleep`; Trio is not supported. The stdio CLI and examples use `asyncio.run`.

集成测试固定 SDK 2.3.0，覆盖上述两个客户端模式及真实子进程；取消测试会等待模拟器实际记录到取消，再继续下一步，不依赖随意等待一段时间。依赖允许后续 2.x 版本，但不代表已验证所有版本或所有客户端。它也不是 MCP 规范一致性检查器或模型能力评测。

异步运行当前要求 **asyncio**；模拟器使用 `asyncio.sleep`，不支持 Trio。命令行与示例均通过 `asyncio.run` 运行。

Run locally with the optional extra installed:

```sh
python -m unittest discover -s tests -v
```

Without the extra, integration tests are skipped; base behavior and adapter contract/install-hint tests still run. The CI has separate base and SDK integration jobs, so an accidentally imported optional dependency does not go unnoticed.

Implementation references: [SDK v2 migration](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/migration.md), [official client testing](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/get-started/testing.md), and [error handling](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/servers/handling-errors.md). Fixture text/results are untrusted data for an agent caller and are not redacted; use synthetic test data and an appropriate client environment.
