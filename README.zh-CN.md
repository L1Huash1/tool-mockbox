# tool-mockbox

[English](README.md) · [本地 HTTP 协议](docs/protocol.md) · [可选 MCP 接入](docs/mcp.md)

**让工具按预设顺序失败或成功，检查 Agent 周边代码能否正确处理。**

例如，你想测试「搜索工具失败两次，第三次成功」时的重试逻辑。这里把三次响应写进 JSON，本地 Python 代码就能调用它，并生成可打开查看的调用报告。

它不需要模型、密钥或付费 API。默认 Python / HTTP 模式没有第三方运行依赖。当前是 `0.2.1` 早期开发工具，可选 MCP 接入需要官方 Python SDK v2。它模拟的是**工具调用的输入与返回值**，不会调用真实上游工具，也不是网络故障模拟器或大模型能力评测。

**0.2 新增：**通过标准输入输出让真实 MCP 客户端调用这些虚构工具，并保留结构化返回和取消语义。在源码目录运行 `python -m pip install ".[mcp]"` 后，可试 `python examples/mcp_client.py`。[接入教程](docs/mcp.md)说明了配置、场景限制和实际验证版本。

**0.2.1 重试示例：**`python examples/mcp_retry_client.py --max-attempts 3` 展示由客户端决定的有界重试：只有格式正确且错误码为 `temporary` 的工具失败才会重试。输出 JSON 会保留成功前发生的失败；协议错误和其他工具错误立即停止。设为 `--max-attempts 2` 会退出 1 并说明预算耗尽。这与固定调用三次的协议检查、按列表重放请求的 `run` 命令不同，详见[有界重试](docs/mcp.md#bounded-retries--有界重试)。

## 先运行一个例子

需要 Python **3.10 或更高版本**。当前从源码安装，尚未发布到 PyPI。

```sh
git clone https://github.com/L1Huash1/tool-mockbox.git
cd tool-mockbox
python -m venv .venv
```

**Windows PowerShell**：直接使用环境内程序，不必调整激活脚本权限。

```powershell
.venv\Scripts\python -m pip install .
.venv\Scripts\tool-mockbox run examples/healthy.json examples/healthy-requests.jsonl --output run-output
.venv\Scripts\python examples/retry_demo.py
```

**macOS / Linux**：

```sh
.venv/bin/python -m pip install .
.venv/bin/tool-mockbox run examples/healthy.json examples/healthy-requests.jsonl --output run-output
.venv/bin/python examples/retry_demo.py
```

打开 `run-output/report.html` 就能看到一次成功的模拟查询。重试示例会显示前两次预设失败，第三次返回成功；这段 Python 示例里有真正执行的重试循环，没有调用大模型。

安装时可能需要下载构建工具 `setuptools`；运行模拟工具本身不会发出外部网络请求。示例全部使用虚构数据。

## 观察失败和重试

下面的简写命令假设已激活虚拟环境；也可以使用上面的程序完整路径。

```sh
tool-mockbox run examples/flaky-search.json examples/retry-requests.jsonl --output retry-output
```

这会记录 **两次预设失败、一次成功**，生成 `run.json` 与 `report.html`，退出码为 **1**。这里的 1 表示记录中出现过失败，程序仍然正常生成了报告。`run` 只是按列表重放调用；它不会自己决定是否重试。重试策略请看 `examples/retry_demo.py`。

```sh
tool-mockbox run examples/flaky-search.json examples/exhausted-requests.jsonl --output exhausted-output --no-wait
```

第四次调用已经没有对应的响应，会被拒绝，原因是 `sequence_exhausted`。`--no-wait` 仅跳过预设等待，并会在报告中标明；这些耗时不能代表真实服务延迟。

## 如何编写场景

每个工具拥有自己的有限响应序列：

```json
{
  "schema_version": 1,
  "tools": [{
    "name": "search_notes",
    "steps": [
      {
        "arguments": {"query": "release"},
        "status": "error",
        "error": {"code": "temporary", "message": "模拟临时失败"},
        "delay_ms": 15
      },
      {
        "arguments": {"query": "release"},
        "status": "ok",
        "result": {"notes": ["检查发布清单"]}
      }
    ]
  }]
}
```

调用列表采用 JSONL：每个非空行是一条完整调用。

```json
{"tool":"search_notes","arguments":{"query":"release"}}
```

未知字段、重复 JSON 键、成功与错误字段混用都会被拒绝。JSON 中的字符串不会被当作命令或表达式执行。

## 在自己的 Python 代码中使用

```python
from tool_mockbox import MockToolbox, MockToolError, UnexpectedCall

box = MockToolbox.from_file("examples/flaky-search.json")
for attempt in range(3):
    try:
        result = box.call("search_notes", {"query": "release"})
    except UnexpectedCall:
        raise  # 参数不匹配等问题不应被当成临时服务失败。
    except MockToolError as error:
        if error.code != "temporary":
            raise
    else:
        print(result)
        break
else:
    raise RuntimeError("Retry budget exhausted")

print(box.report()["summary"])
```

asyncio 异步代码使用 `await box.acall(工具名, 参数)`，当前不支持 Trio。`box.catalog()` 返回每个工具已消耗与剩余的步数；`box.report()` 返回独立的 JSON 数据快照。也可自行写入报告：

```python
from pathlib import Path
from tool_mockbox.report import render_report

with Path("python-report.html").open("x", encoding="utf-8") as stream:
    stream.write(render_report(box.report()))
```

匹配和状态规则：

- 参数必须与下一步的**整个 JSON 值**一致。对象的键顺序不影响匹配，数组顺序影响；`1` 与 `1.0` 一致，`true` 与 `1` 不一致。不支持通配符和部分字段匹配。
- 匹配的调用会在等待前预留并消耗一步。预设失败也消耗一步。未知工具、参数不匹配、序列用尽会记录为 `rejected`，不消耗下一步。
- 并发调用按取得锁的顺序预留响应，完成顺序可以不同，等待期间不会占用锁。
- 异步等待时取消任务会记录 `cancelled`，保留已消耗的状态，并继续抛出原始取消异常。异步跳过等待也会让出一次执行机会。任务开始前就被取消则没有调用记录。同步等待被中断也会记录取消并抛出原始异常。
- 不合法输入不产生记录。日志达到上限时，本次调用不会记录或消耗，抛出 `UnexpectedCall(code="event_limit")`。

## 本地 HTTP 模式

一个终端启动：

```sh
tool-mockbox serve examples/healthy.json --port 8769
```

另一个终端使用相同虚拟环境：

```sh
python examples/http_client.py --port 8769
```

服务只监听 `127.0.0.1`，提供 `GET /catalog`、`GET /events`、`POST /call`。端口设为 `0` 时自动选择空闲端口并打印地址。每次重新启动都从全新状态开始；没有重置接口，也不持久保存数据。[协议与状态码说明](docs/protocol.md)给出了具体格式。

这个小服务面向可信的本机开发，没有身份验证、TLS、进程隔离或生产级并发保护。Host / Origin 检查会拒绝外部网页来源，且没有开启跨域访问；同一台电脑上的其他程序仍可访问。请勿把端口转发到公网。

## 报告与边界

`run` 要求一个**尚不存在**的输出目录，避免覆盖之前的报告或输入文件。它不是事务写入：运行被中断或磁盘写入失败时，可能留下部分目录。再次运行请使用新目录名。

HTML 报告支持搜索、状态筛选和匹配数量提示，展示参数、结果、预设等待及实际观察到的耗时。不需要外部资源，也不会上传数据；关闭 JavaScript 后仍可读取所有调用。

参数与返回值**不会自动脱敏**。请使用合适的测试数据，并控制报告的分享范围。

| 限制 | 数值 |
| --- | --- |
| 场景文件 / 整份 JSONL 调用文件 | 各 1 MiB 原始字节 |
| 单个 JSON 快照（包括完整场景） | 1 MiB，按 ASCII 转义后的 JSON 计算 |
| 工具 / 每个工具步数 / 总步数 | 100 / 1,000 / 10,000 |
| JSON 嵌套 / 整数大小 | 64 层 / 1,024 位 |
| 每步等待 | 0–5,000 毫秒 |
| 记录数量 | 最多 20,000 次 |
| 日志预算 | 8 MiB，调用前按保守大小预留 |
| HTTP 调用请求体 | 65,536 字节 |
| HTTP 套接字无数据活动超时 | 5 秒 |

日志预算按紧凑的 ASCII 转义载荷加预留空间计算，**不等于**最终缩进 JSON 或 HTML 文件的大小；最终文件可能更大。中文会占用更多转义空间。JSONL 另有最多 10,000 条调用限制。超限、孤立 Unicode 代理字符、非有限数字以及不能保持支持精度的小数会被拒绝。

| `run` 退出码 | 含义 |
| --- | --- |
| `0` | 所有记录成功，空调用列表也属于成功 |
| `1` | 记录中存在预设失败、拒绝、取消或未完成调用 |
| `2` | 输入、文件读写、用法错误，或日志上限导致提前停止 |

日志用尽时会保存标明「部分运行」的报告并停止。场景自己设置的错误码即使叫 `event_limit`，也不会被误认为日志上限。服务模式通常持续运行，键盘中断返回 `130`。

## 开发与定位

```sh
python -m pip install .
python -m unittest discover -s tests -v
```

基础 CI 在 Windows / Linux、Python 3.10 / 3.13 上运行测试与成功、重试示例。另有四个集成任务安装 MCP SDK 2.3.0，验证官方客户端 auto / legacy 模式和真实 stdio 子进程。未安装可选依赖时跳过集成测试，基础与接入契约测试仍会运行。测试涵盖真实本地 HTTP 请求、取消、并发预留、容量边界与命令行行为。成功的基础 Actions 任务会附 `healthy-report-...` 下载包，只包含虚构示例的 JSON 与 HTML 报告，保留 7 天。在运行页面的 Artifacts 区域下载后，本地打开 `report.html` 即可查看。

如果需要丰富的 HTTP 路由、代理录制或网络故障模拟，应选专门的服务模拟工具。例如 [WireMock](https://wiremock.org/docs/stateful-behaviour/) 已有状态场景功能。本项目聚焦小型 Python 工具调用模拟与清晰的调用记录，不承诺模型质量、安全性或真实性能。

MIT 开源协议。项目使用 AI 辅助开发，代码、示例和测试均可检查。预设的失败与延迟不代表真实上游服务的可靠性。
