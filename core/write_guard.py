"""写接口访问控制（Web 层中间件）

背景
----
WebUI 服务默认监听 ``0.0.0.0``，而 aiohttp 应用没有注册任何 middleware。
全项目只有 ``/api/v1/security/sh`` 一处校验 ``server.auth_token``，
其余写接口（改配置、建定时任务、删知识库文件、写 SKILL.md、注册命令、
调度子代理……）全部没有任何校验。同一局域网内的任何设备都能直接调用。

本模块提供统一的写方法守卫，策略三档，由 ``server.write_guard`` 控制：

``local_only``（默认）
    仅允许回环地址（127.0.0.1 / ::1 / ::ffff:127.0.0.1）执行写操作。
    本机浏览器访问 WebUI 完全不受影响；局域网、ZeroTier 等外部来源一律 403。

``token``
    需要请求头 ``X-Admin-Token`` 或查询参数 ``?token=`` 与 ``server.auth_token``
    一致。适合需要远程管理且愿意配置令牌的场景。
    **若 ``auth_token`` 为空，会自动退化为 ``local_only``**，避免“配了 token 模式
    但忘了填令牌”导致重新裸奔。

``off``
    不拦截，保持加入本模块之前的行为。

放行规则
--------
- 非写方法（GET / HEAD / OPTIONS）直接放行，CORS 预检不会被挡。
- ``passthrough_paths`` 中的路径交给路由自身处理（``/api/v1/security/sh``
  已有自己的令牌校验，不重复拦截，否则远程持令牌的管理员会被挡住）。

中间件签名
----------
aiohttp 3.9+ 的新式中间件是 ``async def middleware(request, handler)``，
但**必须带上 ``__middleware_version__ = 1`` 标记**（用 ``@web.middleware`` 装饰器
等价于设这个属性）。缺少该标记时，aiohttp 会把中间件当成 legacy 形态，以
``await m(app, handler)`` 调用——第一个参数变成 ``Application`` 而不是 ``Request``，
结果就是**所有请求 500**。

早期版本（legacy）则是 ``async def factory(app, handler)`` 返回协程函数。
本模块使用新式签名 + 显式版本标记——本项目运行的 aiohttp 为 3.14，仅支持新式。
写成同步函数会直接 ``SyntaxError: 'await' outside async function``；
漏掉版本标记则运行期全部请求 500。改动此文件后务必跑
``tests/test_write_guard.py``，并至少做一次真实 aiohttp 集成验证。
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional, Set

WRITE_METHODS: Set[str] = {"POST", "PUT", "DELETE", "PATCH"}

#: 视为本机的地址集合。``::ffff:127.0.0.1`` 是 IPv4 映射到 IPv6 时的常见形态。
LOOPBACK_ADDRESSES: Set[str] = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

DEFAULT_MODE = "local_only"
VALID_MODES = ("local_only", "token", "off")


def is_loopback(remote: Optional[str]) -> bool:
    """判断来源地址是否为本机回环地址。"""
    if not remote:
        return False
    return remote.strip().lower() in LOOPBACK_ADDRESSES


def normalize_mode(mode: Optional[str]) -> str:
    """把配置值规范化到合法档位，非法值退回默认（安全优先）。"""
    if not mode:
        return DEFAULT_MODE
    m = str(mode).strip().lower()
    return m if m in VALID_MODES else DEFAULT_MODE


def make_write_guard(
    mode: str = DEFAULT_MODE,
    token: str = "",
    passthrough_paths: Iterable[str] = ("/api/v1/security/sh",),
    logger=None,
    json_response: Optional[Callable] = None,
):
    """构造 aiohttp 写方法守卫中间件。

    参数
    ----
    mode:
        策略档位，见模块文档。非法值按 ``local_only`` 处理。
    token:
        ``token`` 档位下比对的令牌，通常取 ``server.auth_token``。
    passthrough_paths:
        跳过本守卫的路径集合。
    logger:
        用于记录拦截日志的 logger，可为 None。
    json_response:
        aiohttp 的 ``web.json_response``。为 None 时延迟导入，便于测试替换。

    返回
    ----
    ``async def middleware(request, handler)``，可直接放进
    ``web.Application(middlewares=[...])``。
    """
    effective = normalize_mode(mode)
    passthrough: Set[str] = set(passthrough_paths or ())

    # token 模式但没配令牌 → 退化为 local_only，避免假安全
    if effective == "token" and not (token or "").strip():
        if logger is not None:
            logger.warning(
                "write_guard=token 但 server.auth_token 为空，已自动退化为 local_only"
            )
        effective = "local_only"

    def _resp(payload, status):
        if json_response is not None:
            return json_response(payload, status=status)
        from aiohttp import web  # 延迟导入，测试时无需 aiohttp 也能构造

        return web.json_response(payload, status=status)

    async def middleware(request, handler):
        if request.method not in WRITE_METHODS:
            return await handler(request)
        if effective == "off":
            return await handler(request)
        if request.path in passthrough:
            return await handler(request)

        remote = request.remote or ""

        if effective == "token":
            got = request.headers.get("X-Admin-Token", "") or request.query.get("token", "")
            if got and got == token:
                return await handler(request)
            if logger is not None:
                logger.warning(
                    f"拒绝写请求（令牌缺失或不匹配）: "
                    f"{request.method} {request.path} from {remote}"
                )
            return _resp(
                {
                    "error": "需要管理员令牌",
                    "hint": "请通过请求头 X-Admin-Token 或查询参数 ?token= 提供 server.auth_token",
                    "remote": remote,
                },
                403,
            )

        # local_only
        if is_loopback(remote):
            return await handler(request)

        if logger is not None:
            logger.warning(f"拒绝远程写请求: {request.method} {request.path} from {remote}")
        return _resp(
            {
                "error": "远程写操作已被拦截",
                "hint": (
                    "server.write_guard=local_only 时仅允许本机执行写操作；"
                    "如需远程管理，请设置 server.auth_token 并将 write_guard 改为 token"
                ),
                "remote": remote,
            },
            403,
        )

    # 标记为新式中间件：aiohttp 3.9+ 依据该属性决定按
    # middleware(request, handler) 调用，而不是 legacy 的 m(app, handler)。
    # 漏掉这一行会导致所有请求 500（Application 被当成 Request）。
    middleware.__middleware_version__ = 1

    return middleware
