"""Explicit local tools served to one MCP client over stdio."""

from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import asyncio
import hashlib
import json
from pathlib import Path
import threading

from jsonschema import Draft202012Validator
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import ValidationError

from local_agent_tools import __version__, skills
from local_agent_tools.bootstrap import create_application
from local_agent_tools.config import Settings
from local_agent_tools.foundation.sanitize import Fault
from local_agent_tools.foundation.state import lease, private_directory
from local_agent_tools.mcp.protocol import READ, _reply, _result, MAX_WAITERS
from local_agent_tools.mcp.workspace import register_workspace_tools
from local_agent_tools.mcp.environment import register_environment_tools
from local_agent_tools.mcp.projects import register_project_tools
from local_agent_tools.mcp.tasks import register_task_tools
from local_agent_tools.mcp.execution import register_execution_tools


def create_server(config: Settings, state: Path) -> MCPServer:
    application = create_application(config, state)
    waiting = threading.BoundedSemaphore(MAX_WAITERS)

    @asynccontextmanager
    async def lifespan(_server):
        with application.task_observer.running():
            yield {}

    validators: dict[str, Draft202012Validator] = {}
    catalog: dict = {}
    tool_entry: ContextVar[threading.Event | None] = ContextVar("tool_entry", default=None)

    async def load_catalog() -> dict:
        if not validators:
            tools = await mcp.list_tools()
            descriptors = [tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                           for tool in sorted(tools, key=lambda item: item.name)]
            raw = (json.dumps(descriptors, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            validators.update({tool.name: Draft202012Validator(tool.input_schema) for tool in tools})
            catalog.update(sha256=hashlib.sha256(raw).hexdigest(), tool_count=len(tools),
                           source="server_tools_list", client_match="not_observed")
        return dict(catalog)

    async def safe_tool_call(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        await load_catalog()

        def invalid(reason: str, *, handler_entered: bool = False) -> dict:
            # Only pre-handler validation proves this call did not dispatch an effect.
            message = ("The handler did not return a verified result. Read the original "
                       "task/operation before any retry; execution may have occurred."
                       if handler_entered else
                       "The server rejected this call before its handler. Compare the name and inputs "
                       "with tools/list; refresh the client's stored catalog if it differs.")
            result = _result({"error": {"code": "invalid_tool_call", "message": message},
                "diagnostic": {"source": "server", "reason": reason,
                    "phase": "handler" if handler_entered else "validation",
                    "current_call_dispatch": "unknown" if handler_entered else "not_attempted",
                    "version": __version__, "catalog_sha256": catalog["sha256"]}}, True).model_dump(
                        mode="json", by_alias=True, exclude_none=True, exclude={"result_type"})
            # Middleware short-circuits bypass the SDK's per-version envelope serializer.
            if ctx.protocol_version == "2026-07-28":
                result.update(resultType="complete", _meta={
                    "io.modelcontextprotocol/serverInfo": {"name": mcp.name, "version": mcp.version}})
            return result

        params = ctx.params
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return invalid("invalid_request")
        validator = validators.get(params["name"])
        arguments = params.get("arguments")
        if validator is None:
            return invalid("unknown_tool")
        if not validator.is_valid({} if arguments is None else arguments):
            return invalid("invalid_arguments")
        # The new contract forbids extra fields before SDK argument filtering.
        guarded = {"inspect_projects", "inspect_context", "find_tasks", "read_task", "wait_task", "start_codex_task", "read_codex_task", "create_task", "control_task", "start_execution", "create_worktree", "cancel_execution", "control_execution",
                   "write_workspace_file", "edit_workspace_file", "apply_workspace_patch"}
        if (params["name"] in guarded or "task_context" in (arguments or {})) and set(arguments or {}) - validator.schema["properties"].keys():
            return invalid("unexpected_arguments")
        # The mutable marker crosses the SDK's copied worker-thread context.
        entered = threading.Event()
        token = tool_entry.set(entered)
        try:
            try:
                response = await call_next(ctx)
            except ValidationError:
                return invalid("handler_validation" if entered.is_set() else "sdk_validation",
                               handler_entered=entered.is_set())
            if (isinstance(response, dict) and response.get("isError") is True
                    and response.get("structuredContent") is None):
                return invalid("handler_error" if entered.is_set() else "sdk_validation",
                               handler_entered=entered.is_set())
            return response
        finally:
            tool_entry.reset(token)

    mcp = MCPServer(
        "local-agent-tools", instructions="Read inspect_skills(action='read') for the operating overview. "
        "Each tool's Guide gives an exact bounded read for its instructions; guides never grant permission.",
        version=__version__, middleware=[safe_tool_call], lifespan=lifespan,
    )

    def guided_tool(*, annotations):
        def register(operation):
            operation.__doc__ = skills.describe_tool(operation.__name__, operation.__doc__ or "")
            def mark_entry():
                if (entered := tool_entry.get()) is not None:
                    entered.set()

            if asyncio.iscoroutinefunction(operation):
                @wraps(operation)
                async def handler(*args, **kwargs):
                    mark_entry()
                    return await operation(*args, **kwargs)
            else:
                @wraps(operation)
                def handler(*args, **kwargs):
                    mark_entry()
                    return operation(*args, **kwargs)
            return mcp.tool(annotations=annotations, structured_output=False)(handler)
        return register

    private_directory(state)
    private_directory(state / "locks")

    machine_read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    file_write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

    @guided_tool(annotations=READ)
    async def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        advertised = await load_catalog()
        return await asyncio.to_thread(_reply, lambda: {
            **application.get_capabilities(), "mcp_catalog": advertised})

    register_environment_tools(application, guided_tool, machine_read)

    register_project_tools(application, guided_tool)

    register_workspace_tools(application, guided_tool, machine_read, file_write)

    register_task_tools(application, guided_tool, waiting)

    register_execution_tools(application, guided_tool, waiting, machine_read)

    return mcp


def serve(config: Settings, state: Path) -> None:
    """One server per state directory: its Task observer is the only one there. The client owns
    this server's lifetime through stdin."""
    state = private_directory(state)
    with ExitStack() as held:
        try:
            held.enter_context(lease(private_directory(state / "locks") / "server.lock"))
        except Fault as error:
            if error.code != "resource_busy":
                raise
            raise Fault("state_in_use", "Another local-agent-tools server is using this state directory; "
                        "give each client its own --state-dir.") from error
        create_server(config, state).run(transport="stdio")
