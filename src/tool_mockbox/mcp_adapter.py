"""Optional MCP Python SDK v2 stdio adapter for the existing fixture engine.

Importing this module does not import the optional SDK. Python and HTTP
fixtures keep their wider JSON argument support; MCP requires object arguments.
"""

from importlib.metadata import PackageNotFoundError, version
import json
import re

from . import __version__
from .core import MockToolbox, MockToolError, ScenarioError, UnexpectedCall


INSTALL_HINT = (
    "MCP mode requires the optional MCP Python SDK >=2.3,<3. "
    "From the cloned tool-mockbox source directory, run: "
    'python -m pip install ".[mcp]"'
)


class MCPDependencyError(RuntimeError):
    """The optional compatible SDK is unavailable in this Python environment."""


def _require_sdk():
    try:
        installed = version("mcp")
        release = re.match(r"^(\d+)\.(\d+)(?:\.|$)", installed)
        if not release or int(release[1]) != 2 or int(release[2]) < 3:
            raise MCPDependencyError(INSTALL_HINT + f" Installed SDK: {installed}.")
        from mcp import MCPError
        from mcp.server import Server
        from mcp import types
    except (PackageNotFoundError, ImportError) as error:
        raise MCPDependencyError(INSTALL_HINT) from error
    return MCPError, Server, types


def validate_mcp_fixture(toolbox: MockToolbox) -> None:
    """Reject fixture contracts that MCP cannot preserve without conversion."""
    if not isinstance(toolbox, MockToolbox):
        raise TypeError("toolbox must be a MockToolbox")
    # This package-local adapter reads immutable, validated configuration only.
    # Runtime positions/events are left to the core's synchronized public API.
    for tool in toolbox._tools.values():
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool["name"]) is None:
            raise ScenarioError("MCP tool names must use 1-128 ASCII letters, digits, underscores, dots, or hyphens")
        if any(not isinstance(step["arguments"], dict) for step in tool["steps"]):
            raise ScenarioError(f"MCP tool {tool['name']!r}: every step's arguments must be a JSON object")


def create_mcp_server(toolbox: MockToolbox):
    """Create a low-level SDK Server delegating calls to ``toolbox.acall``.

    The fixture is stateful across all clients using this server instance.
    Success text and structured content use the same {"result": value} wrapper.
    Cancellation is intentionally not caught here.
    """
    validate_mcp_fixture(toolbox)
    MCPError, Server, types = _require_sdk()

    async def list_tools(context, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=tool["name"], description=tool["description"], input_schema={"type": "object"})
            for tool in toolbox.catalog()["tools"]
        ])

    async def call_tool(context, params):
        arguments = params.arguments if params.arguments is not None else {}
        if not isinstance(arguments, dict):
            raise MCPError(types.INVALID_PARAMS, "MCP arguments must be a JSON object",
                           data={"fixture_code": "invalid_request", "call_id": None})
        try:
            result = await toolbox.acall(params.name, arguments)
        except UnexpectedCall as error:
            raise MCPError(types.INVALID_PARAMS, error.message,
                           data={"fixture_code": error.code, "call_id": error.call_id}) from error
        except MockToolError as error:
            payload = {"error": {"code": error.code, "message": error.message}, "call_id": error.call_id}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=True))],
                                        is_error=True)
        except ScenarioError as error:
            raise MCPError(types.INVALID_PARAMS, "Unsupported fixture request arguments",
                           data={"fixture_code": "invalid_request", "call_id": None}) from error
        payload = {"result": result}
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=True))],
                                    structured_content=payload)

    return Server("tool-mockbox", version=__version__,
                  instructions="Synthetic local tool fixtures. Responses follow a finite scripted sequence; no real tools are executed.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve_stdio(toolbox: MockToolbox) -> None:
    """Serve MCP over standard input/output without printing a banner."""
    server = create_mcp_server(toolbox)
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as error:
        raise MCPDependencyError(INSTALL_HINT) from error
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
