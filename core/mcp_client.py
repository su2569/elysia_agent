"""MCP Client - Model Context Protocol 客户端连接器（阶段二核心模块）

让 Elysia Agent 成为 MCP Client：连接外部 MCP Server，自动发现远端工具
（tools/list）并包装为 ITool 注册进 ToolRegistry；LLM 调用时经 JSON-RPC 2.0
转发到远端执行（tools/call）。

实现特点（零新增依赖）：
- 纯 aiohttp + 标准库实现 MCP 协议（JSON-RPC 2.0 over stdio / http / sse）
- stdio 传输：asyncio 子进程 + 换行分隔 JSON（MCP stdio 规范）
- streamable-http 传输（2025-03-26+）：单端点 POST，支持 application/json 与
  text/event-stream 两种响应，自动维护 Mcp-Session-Id
- sse 传输（2024-11-05 遗留）：GET 长连接收消息 + endpoint 事件发现 + POST 回发
- initialize 握手 + 协议版本协商（客户端请求 2024-11-05，接受服务器协商版本）

防御性默认值（对齐项目防御基线哲学）：
- stdio 命令注入校验（shell 元字符黑名单，可配置关闭）
- 每服务器工具数上限（防恶意服务器淹没注册表）
- 工具名白名单字符校验（[A-Za-z0-9_-]，前缀 server__ 隔离命名空间）
- 全链路超时（连接 / 请求），未决请求在 close 时统一失败清理
- MCP 连接失败只降级告警，绝不拖垮主进程（插件可选原则）

协议参考: https://modelcontextprotocol.io/
"""
import asyncio
import itertools
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp 是核心依赖
    aiohttp = None

from ..interfaces.tool import ITool
from ..registry.tool_registry import ToolRegistry
from ..utils.logger import get_logger

logger = get_logger("MCPClient")

# 版本信息（向服务器声明的 clientInfo）
try:
    from ..version import __version__ as _AGENT_VERSION
except Exception:  # pragma: no cover
    _AGENT_VERSION = "bate_2.5.0"

# 客户端默认请求的协议版本（最广泛实现验证过的基线；服务器可协商返回其他版本）
MCP_PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC 2.0 标准错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# JSON-RPC 错误码 → MCP 连接级错误（需要重连）
_FATAL_JSONRPC_CODES = {
    JSONRPC_PARSE_ERROR,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
}


class MCPError(Exception):
    """MCP 客户端基础异常"""


class MCPConnectionError(MCPError):
    """连接失败 / 连接中断 / 会话失效（可重连）"""


class MCPTimeoutError(MCPError):
    """请求超时"""


class MCPProtocolError(MCPError):
    """协议错误（服务器返回了无法解析的内容）"""


class MCPValidationError(MCPError):
    """配置校验失败（防御拦截）"""


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 消息构造
# ---------------------------------------------------------------------------

def rpc_request(msg_id: int, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def rpc_notification(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def rpc_response(msg_id: int, result: Any = None) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def rpc_error_response(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# SSE 事件流解析（streamable-http 响应与 legacy sse 传输共用）
# ---------------------------------------------------------------------------

async def parse_sse_stream(content, on_event: Callable[[str, str], None]) -> None:
    """逐事件解析 SSE 流。

    content: aiohttp 响应体（支持 async for line 迭代的对象）
    on_event(event_name, data_text): 每个完整事件回调一次
    """
    event_name = "message"
    data_lines: List[str] = []
    async for raw in content:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            # 空行 = 事件边界
            if data_lines:
                on_event(event_name, "\n".join(data_lines))
            event_name, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue  # 注释/心跳
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------

class ITransport(ABC):
    """MCP 传输抽象。

    约定：
    - start() 完成后即可 send()/接收消息
    - 收到的 JSON-RPC 消息经 on_message 回调上抛（由 MCPClient 注入）
    - send() 同时用于请求 / 通知 / 响应（HTTP 类传输即 POST）
    - close() 幂等
    """

    def __init__(self) -> None:
        self.on_message: Optional[Callable[[Dict[str, Any]], None]] = None

    @abstractmethod
    async def start(self) -> None:
        """建立连接（失败抛 MCPConnectionError）"""

    @abstractmethod
    async def send(self, message: Dict[str, Any]) -> None:
        """把一条 JSON-RPC 消息发往服务器"""

    @abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源"""

    def _dispatch(self, message: Dict[str, Any]) -> None:
        if self.on_message is not None:
            self.on_message(message)


class StdioTransport(ITransport):
    """stdio 传输：拉起子进程，stdin/stdout 上换行分隔 JSON-RPC。

    对应配置: transport: stdio, command / args / env / cwd
    """

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._command = command
        self._args = list(args or [])
        self._env = env or {}
        self._cwd = cwd
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            full_env = None
            if self._env:
                full_env = {**os.environ, **{str(k): str(v) for k, v in self._env.items()}}
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
                cwd=self._cwd,
            )
        except FileNotFoundError:
            raise MCPConnectionError(f"MCP stdio 命令不存在: {self._command}")
        except PermissionError:
            raise MCPConnectionError(f"MCP stdio 命令无执行权限: {self._command}")
        except OSError as e:
            raise MCPConnectionError(f"MCP stdio 启动失败: {e}")
        self._reader_task = asyncio.create_task(self._read_stdout(), name="mcp-stdio-read")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="mcp-stdio-stderr")

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF：服务器进程退出
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.warning(f"MCP stdio 收到无法解析的行: {e}")
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - 读循环兜底
            logger.debug(f"MCP stdio 读循环退出: {e}")
        finally:
            # stdout 关闭视为服务器退出，通知客户端（由 _dispatch 上层处理 pending 失败）
            if not self._closed:
                self._dispatch({"__stream_closed__": True})

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug(f"[mcp-server stderr] {text}")
        except asyncio.CancelledError:
            pass

    async def send(self, message: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPConnectionError("MCP stdio 连接未建立")
        if self._proc.returncode is not None:
            raise MCPConnectionError(f"MCP 服务器进程已退出 (code={self._proc.returncode})")
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise MCPConnectionError("MCP 服务器管道已断开")

    async def close(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = self._stderr_task = None
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except ProcessLookupError:
                pass
            except Exception:  # pragma: no cover
                pass


class StreamableHTTPTransport(ITransport):
    """streamable-http 传输（MCP 2025-03-26+）。

    对应配置: transport: http, url / headers
    - 请求 POST 到单端点；响应为 application/json 或 text/event-stream
    - initialize 响应头捕获 Mcp-Session-Id，后续请求自动携带
    - 服务器在流中主动下发的通知/请求经 on_message 上抛
    """

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 60.0) -> None:
        super().__init__()
        if aiohttp is None:
            raise MCPError("streamable-http 传输需要 aiohttp: pip install aiohttp")
        self._url = url
        self._extra_headers = dict(headers or {})
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_id: Optional[str] = None
        self._closed = False

    async def start(self) -> None:
        if self._session is not None:
            return
        self._session = aiohttp.ClientSession(timeout=self._timeout)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._extra_headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST 一条消息；返回与本请求对应的响应（通知返回 None）。

        SSE 流上先于响应到达的服务器消息会被上抛给 on_message。
        """
        assert self._session is not None, "未 start()"
        try:
            async with self._session.post(
                self._url, json=message, headers=self._headers()
            ) as resp:
                if resp.status == 202:
                    return None  # 通知已被受理
                if resp.status == 404:
                    raise MCPConnectionError("MCP 会话已失效（404），需要重连")
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise MCPConnectionError(f"MCP HTTP {resp.status}: {body}")
                # initialize 响应携带会话 ID
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                content_type = resp.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    return await self._drain_sse(resp, message)
                # 纯 JSON 响应
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    # 服务器主动请求（带 method + id）也可能出现在 JSON 响应
                    if "method" in data and ("result" not in data and "error" not in data):
                        self._dispatch(data)
                        return None
                    return data
                raise MCPProtocolError(f"MCP HTTP 返回非对象 JSON: {type(data).__name__}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if isinstance(e, MCPError):
                raise
            raise MCPConnectionError(f"MCP HTTP 请求失败: {e}") from e

    async def _drain_sse(self, resp, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """读 POST 返回的 SSE 流，直到拿到与请求 id 匹配的响应或流结束。"""
        target_id = request.get("id")
        matched: Optional[Dict[str, Any]] = None

        def _on_event(event_name: str, data_text: str) -> None:
            nonlocal matched
            if event_name not in ("message", "response"):
                return  # endpoint 等事件不属于该传输
            try:
                message = json.loads(data_text)
            except json.JSONDecodeError:
                return
            if not isinstance(message, dict):
                return
            if target_id is not None and message.get("id") == target_id:
                matched = message
            else:
                # 服务器主动下发的通知 / 请求
                self._dispatch(message)

        await parse_sse_stream(resp.content, _on_event)
        return matched  # 通知 POST 的流可能不含任何响应 → None

    async def send(self, message: Dict[str, Any]) -> None:
        """发送通知 / 对服务器请求的响应（POST 后不等待匹配响应）。"""
        await self._post(message)

    async def request(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """发送请求并等待匹配响应（同一条 POST 的返回流内完成关联）。"""
        result = await self._post(message)
        if result is None:
            # 服务器接受了请求但流内未给响应 → 视为协议违规（通知除外）
            raise MCPProtocolError("MCP HTTP 响应流中缺少与请求匹配的响应")
        return result

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


class SseTransport(ITransport):
    """legacy SSE 传输（MCP 2024-11-05）。

    对应配置: transport: sse, url / headers
    - GET url 建立长连接，等待 `endpoint` 事件拿到回发地址
    - 后续所有消息（含响应）经 GET 流下发，请求经 POST {endpoint} 回发
    """

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None,
                 connect_timeout: float = 30.0) -> None:
        super().__init__()
        if aiohttp is None:
            raise MCPError("sse 传输需要 aiohttp: pip install aiohttp")
        self._url = url
        self._extra_headers = dict(headers or {})
        self._connect_timeout = connect_timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._read_task: Optional[asyncio.Task] = None
        self._endpoint: Optional[str] = None
        self._endpoint_ready: Optional[asyncio.Event] = None
        self._closed = False

    async def start(self) -> None:
        if self._session is not None:
            return
        self._session = aiohttp.ClientSession()
        self._endpoint_ready = asyncio.Event()
        self._read_task = asyncio.create_task(self._read_stream(), name="mcp-sse-read")
        try:
            await asyncio.wait_for(self._endpoint_ready.wait(), timeout=self._connect_timeout)
        except asyncio.TimeoutError:
            await self.close()
            raise MCPConnectionError(f"MCP SSE 未在 {self._connect_timeout}s 内下发 endpoint 事件")

    def _resolve_endpoint(self, endpoint: str) -> str:
        """endpoint 事件可能是绝对路径或完整 URL，统一解析为可 POST 的地址。"""
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        # 相对路径：拼到原 URL 的根
        from urllib.parse import urlsplit
        parts = urlsplit(self._url)
        root = f"{parts.scheme}://{parts.netloc}"
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return root + endpoint

    async def _read_stream(self) -> None:
        assert self._session is not None
        try:
            async with self._session.get(
                self._url,
                headers={"Accept": "text/event-stream", **self._extra_headers},
            ) as resp:
                if resp.status >= 400:
                    raise MCPConnectionError(f"MCP SSE 连接失败: HTTP {resp.status}")
                await parse_sse_stream(resp.content, self._on_event)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if not self._closed:
                logger.warning(f"MCP SSE 长连接断开: {e}")
        finally:
            if not self._closed:
                self._dispatch({"__stream_closed__": True})

    def _on_event(self, event_name: str, data_text: str) -> None:
        if event_name == "endpoint" and self._endpoint is None:
            self._endpoint = self._resolve_endpoint(data_text.strip())
            if self._endpoint_ready is not None:
                self._endpoint_ready.set()
            return
        if event_name != "message":
            return
        try:
            message = json.loads(data_text)
        except json.JSONDecodeError:
            return
        if isinstance(message, dict):
            self._dispatch(message)

    async def send(self, message: Dict[str, Any]) -> None:
        if self._endpoint is None:
            raise MCPConnectionError("MCP SSE endpoint 尚未就绪")
        assert self._session is not None
        try:
            async with self._session.post(
                self._endpoint,
                json=message,
                headers={"Content-Type": "application/json", **self._extra_headers},
            ) as resp:
                # 202 = 已受理；部分实现返回 200 也放行
                if resp.status not in (200, 202):
                    body = (await resp.text())[:200]
                    raise MCPConnectionError(f"MCP SSE 回发失败: HTTP {resp.status} {body}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if isinstance(e, MCPError):
                raise
            raise MCPConnectionError(f"MCP SSE 回发失败: {e}") from e

    async def close(self) -> None:
        self._closed = True
        task, self._read_task = self._read_task, None
        if task is not None and not task.done():
            task.cancel()
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


# ---------------------------------------------------------------------------
# MCP 客户端（单服务器连接生命周期）
# ---------------------------------------------------------------------------

class MCPClient:
    """单个 MCP 服务器的客户端连接。

    生命周期: connect() → list_tools()/call_tool()/ping() → close()
    """

    CLIENT_INFO = {"name": "elysia-agent", "version": _AGENT_VERSION}

    def __init__(
        self,
        name: str,
        transport: ITransport,
        connect_timeout: float = 30.0,
        request_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self._transport = transport
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._id_counter = itertools.count(1)
        self._pending: Dict[Any, asyncio.Future] = {}
        self._closed = False
        self.server_info: Dict[str, Any] = {}
        self.negotiated_version: str = MCP_PROTOCOL_VERSION
        self.server_capabilities: Dict[str, Any] = {}
        transport.on_message = self._handle_message

    # -- 连接管理 ----------------------------------------------------------

    async def connect(self) -> None:
        """建立连接并完成 initialize 握手（失败时自动清理传输）。"""
        if self._closed:
            raise MCPConnectionError("客户端已关闭")
        try:
            await self._transport.start()
            init_params = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},  # 客户端侧不需要服务器感知的工具能力
                },
                "clientInfo": self.CLIENT_INFO,
            }
            result = await self._request("initialize", init_params, timeout=self._connect_timeout)
            self.negotiated_version = str(result.get("protocolVersion", MCP_PROTOCOL_VERSION))
            self.server_info = dict(result.get("serverInfo") or {})
            self.server_capabilities = dict(result.get("capabilities") or {})
            await self._notify("notifications/initialized")
        except Exception:
            # 握手失败 → 清理半开状态，保证 connected=False、可安全 close
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MCPConnectionError("MCP 握手失败"))
            self._pending.clear()
            try:
                await self._transport.close()
            except Exception:
                pass
            raise
        logger.info(
            f"MCP[{self.name}] 已连接: server={self.server_info.get('name', '?')} "
            f"v{self.server_info.get('version', '?')} (协议 {self.negotiated_version})"
        )

    async def close(self) -> None:
        """关闭连接，未决请求统一失败。"""
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPConnectionError("MCP 客户端已关闭"))
        self._pending.clear()
        await self._transport.close()

    @property
    def connected(self) -> bool:
        return not self._closed

    # -- MCP 方法 ----------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        """tools/list：返回服务器工具描述列表。"""
        result = await self._request("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise MCPProtocolError("tools/list 返回的 tools 不是列表")
        return [t for t in tools if isinstance(t, dict)]

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """tools/call：调用远端工具，返回 {"content": [...], "isError": bool}。"""
        result = await self._request(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("tools/call 返回结构异常")
        return result

    async def ping(self) -> bool:
        """ping：往返健康检查。"""
        await self._request("ping", {})
        return True

    # -- 内部：请求/响应关联 ------------------------------------------------

    async def _request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        msg_id = next(self._id_counter)
        message = rpc_request(msg_id, method, params)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            # streamable-http 在同一条 POST 内完成请求-响应关联
            if isinstance(self._transport, StreamableHTTPTransport):
                response = await asyncio.wait_for(
                    self._transport.request(message), timeout or self._request_timeout
                )
                self._raise_if_error(response, method)
                return response.get("result", {})
            await self._transport.send(message)
            return await asyncio.wait_for(fut, timeout or self._request_timeout)
        except asyncio.TimeoutError:
            raise MCPTimeoutError(f"MCP[{self.name}] {method} 超时 ({timeout or self._request_timeout}s)")
        finally:
            self._pending.pop(msg_id, None)

    async def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        await self._transport.send(rpc_notification(method, params))

    def _raise_if_error(self, response: Dict[str, Any], method: str) -> None:
        error = response.get("error")
        if isinstance(error, dict):
            code = error.get("code", JSONRPC_INTERNAL_ERROR)
            message = error.get("message", "未知错误")
            if code in _FATAL_JSONRPC_CODES:
                raise MCPProtocolError(f"MCP[{self.name}] {method} 失败: [{code}] {message}")
            raise MCPError(f"MCP[{self.name}] {method} 失败: [{code}] {message}")

    def _handle_message(self, message: Dict[str, Any]) -> None:
        """传输层上抛的所有 JSON-RPC 消息统一入口。"""
        # 连接级信号（stdio EOF / SSE 断开）
        if "__stream_closed__" in message:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MCPConnectionError("MCP 服务器连接已断开"))
            self._pending.clear()
            return
        # JSON-RPC 响应（关联到 pending 请求）
        if "id" in message and ("result" in message or "error" in message):
            fut = self._pending.get(message["id"])
            if fut is not None and not fut.done():
                error = message.get("error")
                if isinstance(error, dict):
                    fut.set_exception(
                        MCPError(
                            f"MCP[{self.name}] 请求失败: "
                            f"[{error.get('code')}] {error.get('message')}"
                        )
                    )
                else:
                    fut.set_result(message.get("result", {}))
            return
        # 服务器主动请求：自动应答 ping，其余记录日志（工具列表变更等通知不处理）
        method = message.get("method")
        if method == "ping" and "id" in message:
            asyncio.ensure_future(self._reply_pong(message["id"]))
        elif method == "notifications/tools/list_changed":
            logger.info(f"MCP[{self.name}] 服务器工具列表已变更（下次 load_all 时刷新）")
        elif method:
            logger.debug(f"MCP[{self.name}] 服务器通知: {method}")

    async def _reply_pong(self, msg_id: Any) -> None:
        try:
            await self._transport.send(rpc_response(msg_id, {}))
        except Exception as e:  # pragma: no cover
            logger.debug(f"MCP[{self.name}] 应答 ping 失败: {e}")


# ---------------------------------------------------------------------------
# 工具包装：远端工具 → ITool
# ---------------------------------------------------------------------------

def _format_content(content: List[Any]) -> str:
    """把 tools/call 的 content 数组聚合为文本。

    text 项拼接；image/audio/resource 等非文本项给出占位标记。
    """
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "text")
        if item_type == "text":
            parts.append(str(item.get("text", "")))
        elif item_type == "image":
            parts.append("[图片内容已省略（base64）]")
        elif item_type == "audio":
            parts.append("[音频内容已省略]")
        elif item_type == "resource":
            resource = item.get("resource", {})
            parts.append(f"[资源: {resource.get('uri', '?')}]")
        else:
            parts.append(f"[{item_type} 内容]")
    return "\n".join(p for p in parts if p)


class MCPTool(ITool):
    """把远端 MCP 工具包装为本地 ITool（注册进 ToolRegistry）。"""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        parameters: Optional[Dict[str, Any]],
        client: MCPClient,
        category: str = "mcp",
    ) -> None:
        self._server = server_name
        self._tool_name = tool_name
        self._desc = description or ""
        self._params = parameters if isinstance(parameters, dict) else {
            "type": "object", "properties": {},
        }
        self._client = client
        self._category = category

    @property
    def name(self) -> str:
        return f"{self._server}__{self._tool_name}"

    @property
    def description(self) -> str:
        return self._desc

    @property
    def parameters(self) -> Dict[str, Any]:
        return self._params

    @property
    def category(self) -> str:
        return self._category

    @property
    def server_name(self) -> str:
        return self._server

    @property
    def remote_name(self) -> str:
        return self._tool_name

    async def execute(self, **kwargs) -> str:
        # 过滤内部参数（_ctx 等），保留业务参数
        clean = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        ToolRegistry.record_usage(self.name)
        try:
            result = await self._client.call_tool(self._tool_name, clean)
        except MCPTimeoutError as e:
            return f"【MCP 超时】{e}"
        except MCPConnectionError as e:
            return f"【MCP 连接错误】{e}"
        except MCPError as e:
            return f"【MCP 调用失败】{e}"
        if result.get("isError"):
            return f"【远端工具错误】{_format_content(result.get('content', [])) or '未知错误'}"
        content = result.get("content")
        if content is None:
            # 兼容无 content 字段的旧实现
            return json.dumps(result, ensure_ascii=False, default=str)[:4000]
        text = _format_content(content)
        return text if text else "(远端工具无输出)"


# ---------------------------------------------------------------------------
# 多服务器管理
# ---------------------------------------------------------------------------

# 工具/服务器名白名单（防御：防恶意名注入提示词或撞名）
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# stdio command 注入黑名单（shell 元字符——经列表式 exec 本无害，但出现在 command
# 本身几乎必然是配置被注入的迹象）
_SHELL_META_RE = re.compile(r"[;&|<>`\n\r]|\$\(")


class MCPManager:
    """MCP 多服务器管理器。

    - 从配置列表加载服务器 → 连接 → 发现工具 → 注册进 ToolRegistry
    - 单服务器失败只降级告警，不影响其他服务器与主进程
    - shutdown() 反注册全部 MCP 工具并优雅关闭连接

    配置项（MCPServerConfig 或等价 dict）：
    - name: 服务器名（工具前缀，如 filesystem__list_directory）
    - transport: stdio | http | sse
    - stdio: command / args / env / cwd
    - http/sse: url / headers
    - enabled / timeout
    """

    def __init__(
        self,
        servers: Optional[List[Any]] = None,
        strict_command_validation: bool = True,
        max_tools_per_server: int = 200,
        connect_timeout: float = 30.0,
        request_timeout: float = 60.0,
    ) -> None:
        self._server_cfgs: List[Any] = list(servers or [])
        self._strict = strict_command_validation
        self._max_tools = max_tools_per_server
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        # name → {"client": MCPClient, "tools": [MCPTool], "error": str|None}
        self._servers: Dict[str, Dict[str, Any]] = {}

    # -- 构建辅助 ----------------------------------------------------------

    @classmethod
    def from_config(cls, mcp_config: Any) -> "MCPManager":
        """从 Settings.mcp（MCPConfig）构建。"""
        return cls(
            servers=list(getattr(mcp_config, "servers", []) or []),
            strict_command_validation=getattr(mcp_config, "strict_command_validation", True),
            max_tools_per_server=getattr(mcp_config, "max_tools_per_server", 200),
            connect_timeout=float(getattr(mcp_config, "connect_timeout", 30)),
            request_timeout=float(getattr(mcp_config, "request_timeout", 60)),
        )

    def _cfg_get(self, cfg: Any, key: str, default: Any = None) -> Any:
        if isinstance(cfg, dict):
            return cfg.get(key, default)
        return getattr(cfg, key, default)

    def _build_transport(self, cfg: Any, timeout: float) -> ITransport:
        transport = str(self._cfg_get(cfg, "transport", "stdio") or "stdio").lower()
        if transport in ("stdio",):
            command = str(self._cfg_get(cfg, "command", "") or "")
            self._validate_command(command)
            args = self._cfg_get(cfg, "args", []) or []
            env = self._cfg_get(cfg, "env", {}) or {}
            cwd = self._cfg_get(cfg, "cwd", None)
            if cwd:
                cwd = str(cwd)
            return StdioTransport(command, args, env, cwd)
        if transport in ("http", "streamable-http", "streamable_http"):
            url = str(self._cfg_get(cfg, "url", "") or "")
            if not url:
                raise MCPValidationError("http 传输缺少 url")
            headers = self._cfg_get(cfg, "headers", {}) or {}
            return StreamableHTTPTransport(url, headers, timeout=self._request_timeout)
        if transport in ("sse",):
            url = str(self._cfg_get(cfg, "url", "") or "")
            if not url:
                raise MCPValidationError("sse 传输缺少 url")
            headers = self._cfg_get(cfg, "headers", {}) or {}
            return SseTransport(url, headers, connect_timeout=self._connect_timeout)
        raise MCPValidationError(f"未知 MCP 传输类型: {transport}")

    def _validate_command(self, command: str) -> None:
        """防御校验：stdio command 不得含 shell 元字符（防配置注入）。"""
        if not command:
            raise MCPValidationError("stdio 传输缺少 command")
        if not self._strict:
            return
        if _SHELL_META_RE.search(command):
            raise MCPValidationError(
                f"stdio command 含 shell 元字符，疑似命令注入，已拦截: {command!r}"
                "（如确需使用，可设 strict_command_validation: false）"
            )

    def _sanitize_server_name(self, raw: str, index: int) -> str:
        name = (raw or "").strip()
        if not _SAFE_NAME_RE.fullmatch(name):
            name = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or f"server{index}"
        # 去重：重名服务器追加序号
        base, final, n = name, name, 1
        while final in self._servers:
            n += 1
            final = f"{base}{n}"
        return final

    # -- 生命周期 ----------------------------------------------------------

    async def load_all(self) -> Dict[str, Any]:
        """按配置逐台连接并注册工具。返回各服务器加载结果摘要。"""
        for i, cfg in enumerate(self._server_cfgs):
            if not self._cfg_get(cfg, "enabled", True):
                continue
            await self.load_server(cfg, index=i)
        return self.status()

    async def load_server(self, cfg: Any, index: int = 0, name_override: Optional[str] = None) -> None:
        """连接单台服务器并注册其工具（失败降级，不抛异常）。"""
        if name_override:
            name = name_override  # 已在首次加载时清洗过的名字（reload 复用）
        else:
            raw_name = str(self._cfg_get(cfg, "name", "") or f"server{index}")
            name = self._sanitize_server_name(raw_name, index)
        entry: Dict[str, Any] = {"client": None, "tools": [], "error": None, "config": cfg}
        self._servers[name] = entry
        try:
            timeout = float(self._cfg_get(cfg, "timeout", self._request_timeout) or self._request_timeout)
            transport = self._build_transport(cfg, timeout)
            client = MCPClient(
                name, transport,
                connect_timeout=self._connect_timeout,
                request_timeout=timeout,
            )
            await client.connect()
            entry["client"] = client
            tools = await client.list_tools()
            if len(tools) > self._max_tools:
                logger.warning(
                    f"MCP[{name}] 工具数 {len(tools)} 超上限 {self._max_tools}，仅注册前 {self._max_tools} 个"
                )
                tools = tools[: self._max_tools]
            registered = 0
            for tool_def in tools:
                tool_name = str(tool_def.get("name", "") or "")
                if not _SAFE_NAME_RE.fullmatch(tool_name):
                    logger.warning(f"MCP[{name}] 跳过非法工具名: {tool_name!r}")
                    continue
                mcp_tool = MCPTool(
                    server_name=name,
                    tool_name=tool_name,
                    description=str(tool_def.get("description", "") or ""),
                    parameters=tool_def.get("inputSchema"),
                    client=client,
                )
                ToolRegistry.register(mcp_tool)
                entry["tools"].append(mcp_tool)
                registered += 1
            logger.info(f"MCP[{name}] 已注册 {registered} 个远端工具")
        except (MCPError, OSError, ValueError) as e:
            entry["error"] = str(e)
            logger.warning(f"MCP[{name}] 加载失败（已降级跳过）: {e}")
            # 清理半开连接
            client = entry.get("client")
            if client is not None:
                try:
                    await client.close()
                except Exception:  # pragma: no cover
                    pass
                entry["client"] = None

    async def reload_server(self, name: str) -> bool:
        """重连指定服务器（先卸载其工具再重载，复用原名，不重复追加序号）。"""
        entry = self._servers.get(name)
        if entry is None:
            return False
        cfg = entry["config"]
        await self._unload_entry(name, entry)
        self._servers.pop(name, None)  # 先移除占位，让重载使用原名
        await self.load_server(cfg, index=0, name_override=name)
        return True

    async def shutdown(self) -> None:
        """反注册全部 MCP 工具并关闭所有连接。"""
        for name, entry in list(self._servers.items()):
            await self._unload_entry(name, entry)
        self._servers.clear()

    async def _unload_entry(self, name: str, entry: Dict[str, Any]) -> None:
        for tool in entry.get("tools", []):
            try:
                ToolRegistry.unregister(tool.name)
            except Exception:  # pragma: no cover
                pass
        entry["tools"] = []
        client = entry.get("client")
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover
                pass
            entry["client"] = None

    # -- 状态 --------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """各服务器连接状态摘要（供 /status 与 WebUI 使用）。"""
        servers = {}
        total_tools = 0
        for name, entry in self._servers.items():
            client: Optional[MCPClient] = entry.get("client")
            servers[name] = {
                "connected": bool(client and client.connected),
                "tools": len(entry.get("tools", [])),
                "error": entry.get("error"),
                "server_info": client.server_info if client else {},
                "protocol_version": client.negotiated_version if client else "",
            }
            total_tools += len(entry.get("tools", []))
        return {
            "enabled": True,
            "servers": servers,
            "total_tools": total_tools,
        }

    @property
    def server_names(self) -> List[str]:
        return list(self._servers.keys())
