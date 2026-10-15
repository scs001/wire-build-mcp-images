"""产物构建十工具面：事实读 / 建集 / 产物写（草稿优先）/ 状态迁移。

写入纪律（design D2，spec「Draft-first write discipline」）：创建与更新一律落
draft；发布是独立的 ``wire_artifact_publish``。工具**不声明**任何「直接发布」
参数——FastMCP 对未声明参数直接拒绝（实测 4.1.0：unexpected_keyword_argument），
天然兑现「拒收发布开关、不猜语义、不静默忽略」。

列名核对层（design D3，spec「Spec errors name the real columns」）：上游 form
结构层按设计不裁决引用列是否仍在数据集中（留到提交阶段 422），record-view 侧
保存时即拒。本层在**写入失败**时追加一次数据集列事实读取，把真实列名清单拼进
错误文案——失败发生在生成侧而非客户扫码侧。

Bearer 从进站 MCP 请求取（get_http_request 原始头——fastmcp 4.1.0 的
get_http_headers 过滤 Authorization，家族坑）。上游权威鉴权，本层不解析凭据。
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from .client import MISSING_CREDENTIAL_HINT, WireClient, WireError

#: 产物 kind 短名 → 创建工具名（spec：form 与 record-view 两个创建工具）
CREATE_TOOL_KINDS: dict[str, str] = {
    "wire_artifact_form_create": "form",
    "wire_artifact_record_view_create": "record-view",
}

#: 轻量存储层档位码（建集分流判据，与上游 FREE_PLAN_CODE 对表）
FREE_PLAN_CODE = "free"


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


def _spec_dataset_id(spec: object) -> str | None:
    """spec 信封内层的 datasetId（form/record-view 同口径：``spec.spec.datasetId``）。"""
    if not isinstance(spec, dict):
        return None
    inner = spec.get("spec")
    if not isinstance(inner, dict):
        return None
    dataset_id = inner.get("datasetId")
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def _columns_from_dataset(record: object) -> list[str]:
    """数据集详情负载 → 列名清单（GET /datasets/{id} 的 ``columns[].name``）。"""
    if not isinstance(record, dict):
        return []
    columns = record.get("columns")
    if not isinstance(columns, list):
        return []
    names: list[str] = []
    for column in columns:
        if isinstance(column, dict) and isinstance(column.get("name"), str):
            names.append(column["name"])
    return names


async def _annotate_spec_error(
    client: WireClient, *, bearer: str, spec: object, exc: WireError
) -> WireError:
    """spec 写入失败时把目标数据集的真实列名清单拼进错误文案（design D3）。

    仅失败路径发生；数据集 id 提不出或列读取失败时原样返回原错误（绝不因
    辅助信息失败而改写主错误）。
    """
    dataset_id = _spec_dataset_id(spec)
    if not dataset_id:
        return exc
    try:
        record = await client.request(
            "GET", f"/api/v1/datasets/{dataset_id}", bearer=bearer
        )
    except WireError:
        return exc
    names = _columns_from_dataset(record)
    if not names:
        return exc
    hint = (
        f"{exc.detail}\n该数据集当前真实列名（请据此修正 spec 引用）："
        + json.dumps(names, ensure_ascii=False)
    )
    return WireError(exc.kind, exc.status, hint)


def _form_columns_from_spec(spec: object) -> list[str]:
    """form spec 信封内引用的全部字段列（``spec.spec.fields[].column``）。"""
    if not isinstance(spec, dict):
        return []
    inner = spec.get("spec")
    if not isinstance(inner, dict):
        return []
    fields = inner.get("fields")
    if not isinstance(fields, list):
        return []
    return [
        f.get("column") for f in fields
        if isinstance(f, dict) and isinstance(f.get("column"), str) and f.get("column")
    ]


async def _reject_unknown_form_columns(
    client: WireClient, *, bearer: str, spec: object
) -> None:
    """form 列名保存时刻预检（design D3 的 form 侧兑现）。

    上游 form 结构层按设计不裁决引用列（漂移到提交阶段才 422）——那个假设
    对 agent 不成立（无界面下拉可看）。故本层在保存前读一次数据集列事实，
    引用了不存在的列即拒（422 带真实列名清单），绝不落一份「保存成功、提交
    必炸」的 spec。列事实读取失败时不拦（辅助事实不可得，放行交给上游语义）。
    """
    dataset_id = _spec_dataset_id(spec)
    columns = _form_columns_from_spec(spec)
    if not dataset_id or not columns:
        return
    try:
        record = await client.request(
            "GET", f"/api/v1/datasets/{dataset_id}", bearer=bearer
        )
    except WireError:
        return
    real = _columns_from_dataset(record)
    if not real:
        return
    missing = [c for c in dict.fromkeys(columns) if c not in real]
    if missing:
        raise WireError(
            "invalid", 422,
            f"form spec 引用了数据集中不存在的列：{json.dumps(missing, ensure_ascii=False)}"
            "。该数据集当前真实列名（请据此修正 spec 引用）："
            + json.dumps(real, ensure_ascii=False),
        )


def register(mcp: FastMCP, client: WireClient) -> None:
    # ── 事实读（3）────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_schema(dataset_id: str) -> Any:
        """读取数据集列事实（GET /api/v1/datasets/{id}）——列名与类型。

        生成 form/record-view 的 spec 前**先调本工具取事实**，绝不凭记忆编列名；
        schema 预览以列注册表为准（api 层另带实时行数）。跨企业/不存在 404。
        """
        return await client.request(
            "GET", f"/api/v1/datasets/{dataset_id}", bearer=_bearer_or_raise()
        )

    @mcp.tool
    async def wire_artifact_list(
        kind: str | None = None, artifact_status: str | None = None
    ) -> Any:
        """本企业产物列表（GET /api/v1/artifacts，按更新时间倒序）——只读。

        kind 可选过滤（form/record-view/dashboard/alert_rule/…）；artifact_status
        可选 draft/published/archived（不要传 ``status``，那会被 MCP 层当未声明参数拒绝）。
        """
        params: dict[str, Any] = {}
        if kind:
            params["kind"] = kind
        if artifact_status:
            params["status"] = artifact_status
        return await client.request(
            "GET", "/api/v1/artifacts", bearer=_bearer_or_raise(),
            params=params or None,
        )

    @mcp.tool
    async def wire_artifact_get(artifact_id: int) -> Any:
        """产物详情（当前 spec + 状态迁移记录）+ 版本历史——只读。

        两个端点合成一个负载：详情（GET /artifacts/{id}）与版本清单
        （GET /artifacts/{id}/versions）。他企或不存在一律 404（不可区分）。
        """
        bearer = _bearer_or_raise()
        detail = await client.request(
            "GET", f"/api/v1/artifacts/{artifact_id}", bearer=bearer
        )
        versions = await client.request(
            "GET", f"/api/v1/artifacts/{artifact_id}/versions", bearer=bearer
        )
        return {"artifact": detail, "versions": versions}

    # ── 建集（1）──────────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_dataset_create(
        name: str,
        csv_content: str | None = None,
        columns: list[dict[str, Any]] | None = None,
    ) -> Any:
        """按客户存储分层建集——轻量层走 CSV 导入（带行数据）、物化层走列声明（空集）。

        分流判据=客户档位（GET /datasets/quota 的 plan_code）：
        - ``free``（轻量存储层）：**必须**给 ``csv_content``——表头 + 调用方真实的
          行数据；服务**绝不编造示例行**填充。内部两步：POST /datasets/uploads
          （multipart）→ POST /datasets（source.kind=upload）。列与类型由上传解析
          器从内容推断。
        - 其他（引擎物化层）：给 ``columns``（``[{name, type}]``，type ∈
          INTEGER/REAL/TEXT，不确定用 TEXT）走 source.kind=declared 建零行空集；
          给了 ``csv_content`` 则同样走导入路径（带数据建集）。

        轻量存储层被要求走列声明路径时上游 403 档位闸——原样转述（不伪造、不
        静默改道）。配额用尽上游 402/403 如实；删集腾位由调用方决定（服务不代删）。
        """
        bearer = _bearer_or_raise()
        quota = await client.request(
            "GET", "/api/v1/datasets/quota", bearer=bearer
        )
        plan_code = quota.get("plan_code") if isinstance(quota, dict) else None
        is_light = plan_code == FREE_PLAN_CODE

        if is_light:
            if not csv_content:
                raise WireError(
                    "invalid", 422,
                    "轻量存储层建集必须提供 csv_content（表头 + 真实行数据）——"
                    "服务不编造示例行；列类型由内容推断。",
                )
            uploaded = await client.upload_file(
                "/api/v1/datasets/uploads",
                bearer=bearer,
                filename=f"{name}.csv",
                content=csv_content.encode("utf-8"),
            )
            upload_id = uploaded.get("upload_id") if isinstance(uploaded, dict) else None
            if not upload_id:
                return {"created": False, "upload": uploaded,
                        "note": "上传未返回 upload_id——建集未发生"}
            dataset = await client.request(
                "POST", "/api/v1/datasets", bearer=bearer,
                json_body={"name": name, "source": {"kind": "upload", "upload_id": upload_id}},
            )
            return {"created": True, "tier": "api", "upload": uploaded, "dataset": dataset}

        if csv_content:
            uploaded = await client.upload_file(
                "/api/v1/datasets/uploads",
                bearer=bearer,
                filename=f"{name}.csv",
                content=csv_content.encode("utf-8"),
            )
            upload_id = uploaded.get("upload_id") if isinstance(uploaded, dict) else None
            if not upload_id:
                return {"created": False, "upload": uploaded,
                        "note": "上传未返回 upload_id——建集未发生"}
            dataset = await client.request(
                "POST", "/api/v1/datasets", bearer=bearer,
                json_body={"name": name, "source": {"kind": "upload", "upload_id": upload_id}},
            )
            return {"created": True, "tier": "cell", "upload": uploaded, "dataset": dataset}

        if not columns:
            raise WireError(
                "invalid", 422,
                "物化层建集需要 columns（[{name, type}]，type ∈ INTEGER/REAL/TEXT）"
                "——或改给 csv_content 走导入路径。",
            )
        dataset = await client.request(
            "POST", "/api/v1/datasets", bearer=bearer,
            json_body={"name": name, "source": {"kind": "declared", "columns": columns}},
        )
        return {"created": True, "tier": "cell", "dataset": dataset}

    # ── 产物写（3，全落草稿）──────────────────────────────────────────────────

    async def _create_with_spec(kind: str, title: str, spec: dict[str, Any]) -> Any:
        """空白建档（v1 空 spec）→ PUT 完整 spec（v2）——全程 draft。

        上游客户面无「一步落完整 spec v1」的路径：POST /artifacts 只接受
        {kind,title} 并内造最小空 spec；完整 spec 经 PUT 追加为 v2。若 PUT 失败
        （如 spec 引用不存在的列），**补偿归档**刚建的空白产物后抛错——客户面
        零半写。
        """
        bearer = _bearer_or_raise()
        if kind == "form":
            await _reject_unknown_form_columns(client, bearer=bearer, spec=spec)
        artifact = await client.request(
            "POST", "/api/v1/artifacts", bearer=bearer,
            json_body={"kind": kind, "title": title},
        )
        artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
        if artifact_id is None:
            raise WireError("unreachable", None, f"产物创建未返回 id：{artifact!r}")
        try:
            updated = await client.request(
                "PUT", f"/api/v1/artifacts/{artifact_id}", bearer=bearer,
                json_body={"spec": spec},
            )
        except WireError as exc:
            try:
                await client.request(
                    "POST", f"/api/v1/artifacts/{artifact_id}/archive", bearer=bearer
                )
            except WireError:
                pass  # 补偿失败不改写主错误——主错误里已含 artifact_id
            annotated = await _annotate_spec_error(
                client, bearer=bearer, spec=spec, exc=exc
            ) if exc.status == 422 else exc
            raise WireError(
                annotated.kind, annotated.status,
                f"spec 写入被拒（空白产物 {artifact_id} 已补偿归档）：{annotated.detail}",
            ) from exc
        return {
            "artifact_id": artifact_id,
            "kind": kind,
            "title": title,
            "status": updated.get("status") if isinstance(updated, dict) else None,
            "version": updated.get("version") if isinstance(updated, dict) else None,
            "note": "已落草稿（出生 v1 为空版，本次完整 spec 为 v2）；发布请单独调 wire_artifact_publish",
        }

    @mcp.tool
    async def wire_artifact_form_create(title: str, spec: dict[str, Any]) -> Any:
        """创建数据录入表单（kind=form）——接受完整 spec 信封，落 draft。

        spec 信封形状：``{"title": <同 title>, "spec": {"datasetId", "fields": [...],
        "constraints"?: [...]}}``（字段键与校验规则见 fd-wire
        artifacts/form_spec.py——options/optionsFrom/optionsFromDataset 三源恰有
        其一、visibleWhen 系列、fillFrom、computed、constraints 提交闸）。
        生成前先 wire_dataset_schema 取真实列名。datasetId 可为 null（构建中间态，
        可保存不可提交/发布）。

        本工具**只落草稿**：创建即 published 的参数不存在（传了会被 MCP 层拒）。
        """
        return await _create_with_spec("form", title, spec)

    @mcp.tool
    async def wire_artifact_record_view_create(title: str, spec: dict[str, Any]) -> Any:
        """创建单记录扫码页（kind=record-view）——接受完整 spec 信封，落 draft。

        spec 信封形状：``{"title": <同 title>, "spec": {"datasetId", "keyColumn",
        "headerFields": [{"column", "label"?}], "sections": [{"title", "datasetId",
        "dateColumn", "filters"?, "limit"?}]}}``（校验规则见 fd-wire
        artifacts/record_view_spec.py；keyColumn/headerFields.column/dateColumn
        在保存时刻即按数据集列快照校验）。生成前先 wire_dataset_schema。

        本工具**只落草稿**：发布请单独调 wire_artifact_publish。
        """
        return await _create_with_spec("record-view", title, spec)

    @mcp.tool
    async def wire_artifact_update(artifact_id: int, spec: dict[str, Any]) -> Any:
        """更新产物 spec（PUT /artifacts/{id}）——追加新版本行，状态仍为原态。

        archived 产物拒改（上游 409「请先恢复」原文透传——先 wire_artifact_restore）。
        spec 校验按 kind 分派（form/record-view 的结构层 + 属主层）；
        422 时若 spec 引用数据集，错误文案带该数据集真实列名清单。
        """
        bearer = _bearer_or_raise()
        await _reject_unknown_form_columns(client, bearer=bearer, spec=spec)
        try:
            return await client.request(
                "PUT", f"/api/v1/artifacts/{artifact_id}", bearer=bearer,
                json_body={"spec": spec},
            )
        except WireError as exc:
            if exc.status == 422:
                annotated = await _annotate_spec_error(
                    client, bearer=bearer, spec=spec, exc=exc
                )
                raise annotated from exc
            raise

    # ── 状态迁移（3）──────────────────────────────────────────────────────────

    @mcp.tool
    async def wire_artifact_publish(artifact_id: int) -> Any:
        """发布产物（draft → published）——**独立的显式动作**，不做结构校验。

        上游 publish 仅做状态迁移（不校 spec 结构），发布前请确认 spec 已过写入
        校验（创建/更新路径已校）。非法迁移 409 原文透传（如 archived 需先 restore）。
        """
        return await client.request(
            "POST", f"/api/v1/artifacts/{artifact_id}/publish",
            bearer=_bearer_or_raise(),
        )

    @mcp.tool
    async def wire_artifact_archive(artifact_id: int) -> Any:
        """归档产物（draft/published → archived）——不删除，留操作记录。

        归档后不可编辑（PUT 会 409）；如需修改先 wire_artifact_restore 回草稿。
        """
        return await client.request(
            "POST", f"/api/v1/artifacts/{artifact_id}/archive",
            bearer=_bearer_or_raise(),
        )

    @mcp.tool
    async def wire_artifact_restore(artifact_id: int) -> Any:
        """恢复产物（archived → draft）——唯一回退边；恢复后方可再编辑/发布。"""
        return await client.request(
            "POST", f"/api/v1/artifacts/{artifact_id}/restore",
            bearer=_bearer_or_raise(),
        )
