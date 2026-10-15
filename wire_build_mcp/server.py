"""FastMCP 组装 + ASGI 布线（鉴权门 + /health）。"""

from __future__ import annotations

from . import SERVER_NAME, tools_console
from .auth_gate import AuthGate, AuthGateMiddleware
from .client import WireClient
from .config import Settings

INSTRUCTIONS = (
    "柏讯产物构建与治理助手：按 spec 创建/更新/发布数据录入表单（form）与单记录"
    "扫码页（record-view）、读取产物与版本历史、取数据集列事实、按需建集（CSV 导入）。"
    "写入纪律：创建/更新一律落草稿（draft），发布（publish）必须是你**单独显式调用**"
    "一次——绝不假设创建即发布。生成 spec 前先用 wire_dataset_schema 取真实列名，"
    "**绝不编造列名**；spec 引用了不存在的列会被拒绝，错误文案里带该数据集的真实列名"
    "清单，据此当场改。每个调用以你的柏讯凭据执行（Logto ID token 或 wdk_ 数据面 key，"
    "上游双轨判定）。错误如实透传（401=未登录/未映射客户、403=无权限/档位闸、"
    "404=不存在或他企不可见、409=状态冲突、422=校验逐字段）；空结果如实为空。"
)

_HEALTH_OK = b'{"status":"ok"}'


def create_server(settings: Settings | None = None):
    from fastmcp import FastMCP

    settings = settings or Settings.from_env()
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    client = WireClient(settings.base_url, timeout_seconds=settings.timeout_seconds)
    tools_console.register(mcp, client)
    mcp._wire_build_client = client
    mcp._wire_build_settings = settings
    return mcp


def build_app(settings: Settings | None = None):
    """完整 ASGI 应用：/health 无门；其余（/mcp）走鉴权门（无 Bearer 拒）。"""
    mcp = create_server(settings)
    settings_: Settings = mcp._wire_build_settings
    mcp_app = mcp.http_app(path="/mcp")
    gate = AuthGateMiddleware(mcp_app, AuthGate(settings_.auth_cache_ttl_seconds))

    async def app(scope, receive, send):
        if scope.get("type") == "http":
            path = (scope.get("path") or "").rstrip("/")
            if path == "/health":
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": _HEALTH_OK})
                return
        await gate(scope, receive, send)

    return app
