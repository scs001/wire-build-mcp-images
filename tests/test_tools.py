"""工具接线测试：薄转发 + 草稿优先纪律 + 列名核对层 + 建集分流。"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

import wire_build_mcp.tools_console as tc
from wire_build_mcp.client import WireError


class FakeClient:
    """可编排返回的假 client：按序弹出结果，或按 path 分派。"""

    def __init__(self, results=None, by_path=None, error=None):
        self.calls = []
        self.uploads = []
        self.results = list(results or [])
        self.by_path = dict(by_path or {})
        self.error = error

    def _next(self, path):
        if self.error:
            raise self.error
        if path in self.by_path:
            value = self.by_path[path]
            if isinstance(value, Exception):
                raise value
            return value
        if self.results:
            value = self.results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return {}

    async def request(self, method, path, *, bearer, params=None, json_body=None):
        self.calls.append({"method": method, "path": path, "bearer": bearer,
                           "params": params, "json_body": json_body})
        return self._next(f"{method} {path}")

    async def upload_file(self, path, *, bearer, filename, content, content_type="text/csv"):
        self.uploads.append({"path": path, "bearer": bearer, "filename": filename,
                             "content": content})
        return self._next(f"UPLOAD {path}")


@pytest.fixture
def bearer(monkeypatch):
    monkeypatch.setattr(tc, "_current_bearer", lambda: "tok-abc")
    return "tok-abc"


async def _mk(fake):
    mcp = FastMCP("t-wire-build")
    tc.register(mcp, fake)
    return mcp


def _form_spec(dataset_id="ds1"):
    return {"title": "采收录入", "spec": {"datasetId": dataset_id,
            "fields": [{"column": "地块编号", "label": "地块", "input": "text"}]}}


# ── 事实读 ──────────────────────────────────────────────────────────────────

async def test_dataset_schema_thin_forward(bearer):
    fake = FakeClient(by_path={"GET /api/v1/datasets/ds1": {"columns": [{"name": "地块编号"}]}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_schema", {"dataset_id": "ds1"})
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["path"] == "/api/v1/datasets/ds1"
    assert fake.calls[0]["bearer"] == "tok-abc"


async def test_artifact_list_filters(bearer):
    fake = FakeClient(results=[[]])
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_artifact_list", {"kind": "form", "artifact_status": "draft"})
    assert fake.calls[0]["params"] == {"kind": "form", "status": "draft"}


async def test_artifact_get_combines_detail_and_versions(bearer):
    fake = FakeClient(by_path={
        "GET /api/v1/artifacts/7": {"id": 7, "spec": {}},
        "GET /api/v1/artifacts/7/versions": [{"version": 2}, {"version": 1}],
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_artifact_get", {"artifact_id": 7})
    data = out.data if hasattr(out, "data") else out
    assert data["artifact"]["id"] == 7
    assert len(data["versions"]) == 2
    assert len(fake.calls) == 2


# ── 建集分流 ────────────────────────────────────────────────────────────────

async def test_dataset_create_free_tier_upload_path(bearer):
    fake = FakeClient(by_path={
        "GET /api/v1/datasets/quota": {"plan_code": "free", "max_datasets": 3},
        "UPLOAD /api/v1/datasets/uploads": {"upload_id": "u1", "columns": [{"name": "a"}]},
        "POST /api/v1/datasets": {"id": "ds-new", "storage_tier": "api"},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_dataset_create", {
            "name": "采收记录", "csv_content": "a,b\n1,2\n"})
    data = out.data if hasattr(out, "data") else out
    assert data["created"] is True and data["tier"] == "api"
    assert fake.uploads[0]["filename"] == "采收记录.csv"
    body = fake.calls[-1]["json_body"]
    assert body["source"]["kind"] == "upload" and body["source"]["upload_id"] == "u1"


async def test_dataset_create_free_tier_without_csv_refused(bearer):
    fake = FakeClient(by_path={"GET /api/v1/datasets/quota": {"plan_code": "free"}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_dataset_create", {"name": "x"})
    assert "不编造示例行" in str(ei.value)
    assert fake.uploads == []  # 零上传、零建集


async def test_dataset_create_cell_tier_declared_path(bearer):
    fake = FakeClient(by_path={
        "GET /api/v1/datasets/quota": {"plan_code": "basic", "max_datasets": None},
        "POST /api/v1/datasets": {"id": "ds-cell", "storage_tier": "cell"},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_dataset_create", {
            "name": "空集", "columns": [{"name": "a", "type": "TEXT"}]})
    data = out.data if hasattr(out, "data") else out
    assert data["tier"] == "cell"
    body = fake.calls[-1]["json_body"]
    assert body["source"]["kind"] == "declared"
    assert body["source"]["columns"] == [{"name": "a", "type": "TEXT"}]
    assert fake.uploads == []


async def test_dataset_create_cell_tier_with_csv_uses_upload(bearer):
    fake = FakeClient(by_path={
        "GET /api/v1/datasets/quota": {"plan_code": "basic"},
        "UPLOAD /api/v1/datasets/uploads": {"upload_id": "u9"},
        "POST /api/v1/datasets": {"id": "ds-9"},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_dataset_create", {
            "name": "带数建集", "csv_content": "a\n1\n"})
    data = out.data if hasattr(out, "data") else out
    assert data["created"] is True
    assert fake.calls[-1]["json_body"]["source"]["kind"] == "upload"


async def test_dataset_create_cell_tier_without_anything_refused(bearer):
    fake = FakeClient(by_path={"GET /api/v1/datasets/quota": {"plan_code": "basic"}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_dataset_create", {"name": "x"})
    assert "columns" in str(ei.value)


async def test_dataset_create_quota_403_relayed_verbatim(bearer):
    fake = FakeClient(by_path={
        "GET /api/v1/datasets/quota": {"plan_code": "free"},
        "UPLOAD /api/v1/datasets/uploads": {"upload_id": "u1"},
        "POST /api/v1/datasets": WireError(
            "forbidden", 403,
            "声明式建集 需要 cell 物化层（基础档及以上套餐）；升级到基础档及以上套餐后可用"),
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_dataset_create", {
                "name": "x", "csv_content": "a\n1\n"})
    assert "需要 cell 物化层" in str(ei.value)  # 403 升档文案原样，不静默改道


# ── 产物写（草稿优先）────────────────────────────────────────────────────────

async def test_form_create_lands_draft_via_blank_then_put(bearer):
    fake = FakeClient(by_path={
        "POST /api/v1/artifacts": {"id": 11, "kind": "form", "status": "draft", "version": 1},
        "PUT /api/v1/artifacts/11": {"id": 11, "status": "draft", "version": 2},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_artifact_form_create", {
            "title": "采收录入", "spec": _form_spec()})
    data = out.data if hasattr(out, "data") else out
    assert data["artifact_id"] == 11 and data["status"] == "draft"
    assert "wire_artifact_publish" in data["note"]
    assert fake.calls[0]["json_body"] == {"kind": "form", "title": "采收录入"}
    assert fake.calls[1]["json_body"]["spec"]["spec"]["datasetId"] == "ds1"


async def test_record_view_create_lands_draft(bearer):
    fake = FakeClient(by_path={
        "POST /api/v1/artifacts": {"id": 12},
        "PUT /api/v1/artifacts/12": {"id": 12, "status": "draft", "version": 2},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_artifact_record_view_create", {
            "title": "批次扫码页",
            "spec": {"title": "批次扫码页", "spec": {"datasetId": "ds1",
                     "keyColumn": "批次号", "headerFields": [], "sections": []}}})
    assert fake.calls[0]["json_body"]["kind"] == "record-view"


async def test_create_failure_compensates_and_annotates_columns(bearer):
    """spec 写入被拒 → 空白产物补偿归档 + 错误文案带真实列名（design D3）。"""
    fake = FakeClient(by_path={
        "POST /api/v1/artifacts": {"id": 13},
        "PUT /api/v1/artifacts/13": WireError(
            "invalid", 422, "fields[0].column 不在数据集列内"),
        "GET /api/v1/datasets/ds1": {"columns": [{"name": "地块编号"}, {"name": "产量"}]},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_artifact_form_create", {
                "title": "x", "spec": _form_spec()})
    text = str(ei.value)
    assert "补偿归档" in text and "13" in text
    assert "地块编号" in text and "产量" in text  # 真实列名清单在文案里
    assert any(call["path"] == "/api/v1/artifacts/13/archive" for call in fake.calls)


async def test_update_422_annotated_with_real_columns(bearer):
    fake = FakeClient(by_path={
        "PUT /api/v1/artifacts/7": WireError("invalid", 422, "datasetId 他企或不存在"),
        "GET /api/v1/datasets/ds1": {"columns": [{"name": "批次号"}, {"name": "采收日期"}]},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_artifact_update", {"artifact_id": 7, "spec": _form_spec()})
    text = str(ei.value)
    assert "真实列名" in text and "批次号" in text


async def test_update_409_passthrough_no_annotation(bearer):
    fake = FakeClient(by_path={
        "PUT /api/v1/artifacts/7": WireError(
            "conflict", 409, "产物 7 已归档（archived），不可编辑——请先恢复（restore）为草稿再修改。"),
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_artifact_update", {"artifact_id": 7, "spec": _form_spec()})
    assert "请先恢复" in str(ei.value)
    # 409 不做列名核对（只多了一次 PUT 调用）
    assert len(fake.calls) == 1


async def test_publish_is_separate_explicit_call(bearer):
    fake = FakeClient(by_path={
        "POST /api/v1/artifacts/11/publish": {"id": 11, "status": "published", "version": 2},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_artifact_publish", {"artifact_id": 11})
    data = out.data if hasattr(out, "data") else out
    assert data["status"] == "published"
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["path"] == "/api/v1/artifacts/11/publish"


async def test_archive_and_restore(bearer):
    fake = FakeClient(by_path={
        "POST /api/v1/artifacts/11/archive": {"status": "archived"},
        "POST /api/v1/artifacts/11/restore": {"status": "draft"},
    })
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_artifact_archive", {"artifact_id": 11})
        await c.call_tool("wire_artifact_restore", {"artifact_id": 11})
    assert fake.calls[0]["path"].endswith("/archive")
    assert fake.calls[1]["path"].endswith("/restore")


# ── 纪律边界 ────────────────────────────────────────────────────────────────

async def test_create_tool_rejects_publish_switch(bearer):
    """「直接发布」参数不被声明——MCP 层直接拒（不猜语义、不静默忽略）。"""
    fake = FakeClient()
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_artifact_form_create", {
                "title": "x", "spec": _form_spec(), "publish": True})
    assert "publish" in str(ei.value).lower()
    assert fake.calls == []


async def test_missing_credential_hints_and_never_touches_upstream(monkeypatch):
    monkeypatch.setattr(tc, "_current_bearer", lambda: None)
    fake = FakeClient()
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_artifact_list", {})
    assert "wdk_" in str(ei.value)
    assert fake.calls == []
