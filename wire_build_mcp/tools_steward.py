"""数据治理五工具面（wire-ai-data-steward）：读有效视图 / 写修正 / 查历史 / 撤销 / 导出。

## 上游契约（均已核实到 fd-wire 路由）

- ``GET    /api/v1/datasets/{id}/rows?page&page_size``  行预览（有效视图，含修正）
- ``POST   /api/v1/datasets/{id}/corrections``          三分支写修正（insert/update/delete）
- ``GET    /api/v1/datasets/{id}/corrections?include_reverted&include_dangling``
- ``POST   /api/v1/datasets/{id}/corrections/{cid}/revert``
- ``GET    /api/v1/datasets/{id}/export``               导出 CSV（cell 档 10k 闸）

## 两条纪律（design D2/D3）

1. **来源标记强制且不代填**（spec「Corrections are attributable to their actor」）。
   修正写入必须带来源标记；缺失即拒绝，不猜默认值。理由：同一数据集会混入
   「表单提交」与「对话改错」两种来源的数据，标记是事后区分的唯一依据。
2. **导出截断如实**（spec「Export never overstates completeness」）。上游用响应头
   ``X-Export-Truncated: true`` 标注「已截断、可能还有更多行」；HTTP 头对 agent
   不可见，故本层把它 **lift 成返回结构里的显式字段**，避免模型误以为手里
   是全量。

## 一处事实修正（覆盖 spec 初稿）

上游 corrections 端点的审计 actor 由**服务端按客户身份推导**（固定为
``customer:{cid}``），**不接受调用方指定**。故「调用方传 actor」不成立；本层改为
要求「来源标记」，并把它落进载荷供服务端审计使用。

Bearer 从进站 MCP 请求取（get_http_request 原始头——fastmcp 4.1.0 的
get_http_headers 过滤 Authorization，家族坑）。上游权威鉴权，本层不解析凭据。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .client import MISSING_CREDENTIAL_HINT, WireClient, WireError

#: 行分页上限（与上游 page_size 约束一致：1..200）
_PAGE_SIZE_MAX = 200
_PAGE_SIZE_DEFAULT = 50

#: 导出闸（cell 档；上游响应头 X-Export-Truncated 为该闸的权威信号）
_EXPORT_ROW_CAP = 10_000


def _current_bearer() -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_request
    except ImportError:
        return None
    req = get_http_request()
    if req is None:
        return None
    auth = (req.headers.get("Authorization") or req.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _bearer_or_raise() -> str:
    bearer = _current_bearer()
    if not bearer:
        raise WireError("missing_credential", 401, MISSING_CREDENTIAL_HINT)
    return bearer


def _require_source(source: str | None) -> str:
    """来源标记守卫：缺失或空白即拒（不猜、不填默认值）。

    design D2：同一数据集混有表单提交与对话改错两类数据时，该标记是事后
    区分「这笔是谁写的」的唯一依据——代填即等于抹掉可追溯性。
    """
    text = (source or "").strip()
    if not text:
        raise WireError(
            "invalid", 422,
            "写入修正必须提供 source（来源标记，用于事后区分「表单提交」与"
            "「对话改错」两类数据）——服务不代填默认值（代填=抹掉可追溯性）。",
        )
    return text


def register(mcp: FastMCP, client: WireClient) -> None:
    # ── 读（1）─────────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_rows_read(
        dataset_id: str, page: int = 1, page_size: int = _PAGE_SIZE_DEFAULT
    ) -> Any:
        """读取数据集有效视图分页行（GET /datasets/{id}/rows）——只读。

        **读到的是修正生效后的值**（含未回滚的全部修正），非入库原始快照。
        分页由调用方给定：page 从 1 起，page_size ∈ 1..200（默认 50）。
        **不提供「一次取全部」**——要全量走 wire_dataset_export 并接受截断语义。
        """
        if page < 1:
            raise WireError("invalid", 422, "page 必须 ≥ 1")
        if not 1 <= page_size <= _PAGE_SIZE_MAX:
            raise WireError(
                "invalid", 422, f"page_size 必须在 1..{_PAGE_SIZE_MAX} 之间"
            )
        return await client.request(
            "GET", f"/api/v1/datasets/{dataset_id}/rows",
            bearer=_bearer_or_raise(),
            params={"page": page, "page_size": page_size},
        )

    # ── 写（1）─────────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_correct(
        dataset_id: str,
        op: str,
        source: str,
        row_id: str | None = None,
        column: str | None = None,
        value: Any = None,
        values: dict[str, Any] | None = None,
        artifact_id: int | None = None,
    ) -> Any:
        """写入一条修正（POST /datasets/{id}/corrections）——不可变覆盖层。

        三分支（与上游逐字同形）：
        - ``op="update"``：必须带 ``row_id`` 与 ``column``，``value`` 可为 null
        - ``op="delete"``：必须带 ``row_id``
        - ``op="insert"``：必须带 ``values``（整行值）；可选 ``artifact_id``，
          携带时服务端按该 form 产物**当前版本**执行全量字段校验+提交闸
          （与公开提交同一执法点），命中 422 带 constraint id+message 且零写入

        ``source``（**必填**）=来源标记，用于事后区分「这笔是谁写的」——公开表单
        提交的条目的 actor 形如 ``share-form:{artifact_id}``，对话侧写入应带自己的
        可辨识标记（例如 ``conversation:{id}``）。**服务绝不代填**。

        语义边界：基行永不改写（更正以 append 生效）；许可悬空（行锚不存在）
        落层；行数超过档位配额时上游 429/402 如实转述——服务不代删数据。
        """
        if op not in ("insert", "update", "delete"):
            raise WireError("invalid", 422, "op 必须是 insert、update 或 delete 之一")
        payload: dict[str, Any] = {"op": op, "source": _require_source(source)}
        if row_id is not None:
            payload["row_id"] = row_id
        if column is not None:
            payload["column"] = column
        if value is not None:
            payload["value"] = value
        if values is not None:
            payload["values"] = values
        if artifact_id is not None:
            payload["artifact_id"] = artifact_id
        return await client.request(
            "POST", f"/api/v1/datasets/{dataset_id}/corrections",
            bearer=_bearer_or_raise(), json_body=payload,
        )

    # ── 历史（1）───────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_corrections_list(
        dataset_id: str, include_reverted: bool = True, include_dangling: bool = True
    ) -> Any:
        """读取修正历史（GET /datasets/{id}/corrections）——只读、审计留痕。

        默认**包含已回滚项与悬空项**——使「谁在什么时候改了什么、后来又被
        撤销」可完整重放，不静默丢弃；需要干净视图时显式置 false。
        """
        return await client.request(
            "GET", f"/api/v1/datasets/{dataset_id}/corrections",
            bearer=_bearer_or_raise(),
            params={"include_reverted": include_reverted,
                    "include_dangling": include_dangling},
        )

    # ── 撤销（1）───────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_correction_revert(
        dataset_id: str, correction_id: int
    ) -> Any:
        """撤销单条修正（POST .../corrections/{cid}/revert）——停止生效、留档不删。

        重复撤销同一条 SHALL 由上游判 409（原文透传），工具不谎报成功。
        基行永不改写——撤销后有效视图回到修正前的值。
        """
        return await client.request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/corrections/{correction_id}/revert",
            bearer=_bearer_or_raise(),
        )

    # ── 导出（1）───────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_export(dataset_id: str) -> dict[str, Any]:
        """导出数据集有效视图为 CSV（GET /datasets/{id}/export）——只读。

        返回结构含 ``csv`` 文本与 ``truncated`` 布尔：**truncated=true 表示
        结果已被上游行数闸截断（cell 档 10,000 行），可能还有更多行**。调用方
        MUST NOT 把截断结果当全量使用或向终端用户谎报为完整数据。

        CSV 约定沿用上游：UTF-8 带 BOM、RFC4180 转义、表头=列名行。
        """
        import httpx

        bearer = _bearer_or_raise()
        try:
            res = await client._client.request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/export",
                headers={"Authorization": f"Bearer {bearer}"},
            )
        except httpx.HTTPError as exc:
            raise WireError(
                "unreachable", None, f"柏讯服务不可达：{type(exc).__name__}: {exc}"
            ) from exc

        if res.status_code != 200:
            raise WireError(
                {
                    401: "unauthorized", 402: "billing", 403: "forbidden",
                    404: "not_found", 409: "conflict", 422: "invalid",
                    429: "rate_limited",
                }.get(res.status_code, "invalid"),
                res.status_code,
                _extract_detail(res.text),
            )
        return {
            "dataset_id": dataset_id,
            "csv": res.text,
            "truncated": (res.headers.get("X-Export-Truncated") or "").lower() == "true",
            "row_cap": _EXPORT_ROW_CAP,
        }


def _extract_detail(body: str) -> str:
    """错误 body → 文案（优先取 JSON 的 detail/error，否则原文截断）。"""
    import json

    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return str(parsed.get("detail") or parsed.get("error") or body)[:800]
    except ValueError:
        pass
    return (body or "")[:800] or "（无 body）"
