"""治理五工具接线测试：有效视图 / 来源标记强制 / 不可变基行 / 导出截断如实。"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

import wire_build_mcp.tools_steward as ts
from wire_build_mcp.client import WireError


class FakeClient:
    def __init__(self, results=None, by_path=None):
        self.calls = []
        self.results = list(results or [])
        self.by_path = dict(by_path or {})

    def _next(self, path):
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
        return self._next(f"UPLOAD {path}")


@pytest.fixture
def bearer(monkeypatch):
    monkeypatch.setattr(ts, "_current_bearer", lambda: "tok-abc")
    return "tok-abc"


async def _mk(fake):
    mcp = FastMCP("t-steward")
    ts.register(mcp, fake)
    return mcp


# ── 行读取 ──────────────────────────────────────────────────────────────────

async def test_rows_read_pagination(bearer):
    fake = FakeClient(by_path={"GET /api/v1/datasets/ds1/rows": {"rows": []}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_rows_read", {
            "dataset_id": "ds1", "page": 2, "page_size": 25})
    assert fake.calls[0]["params"] == {"page": 2, "page_size": 25}


async def test_rows_read_rejects_bad_pagination(bearer):
    fake = FakeClient()
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        for payload in ({"dataset_id": "ds1", "page": 0},
                        {"dataset_id": "ds1", "page_size": 201}):
            with pytest.raises(Exception):  # noqa: B017 — fastmcp 把工具错误包成 ToolError
                await c.call_tool("wire_dataset_rows_read", payload)
    assert fake.calls == []  # 越界参数零请求


# ── 归因（source 声明性入参，上游不承载）─────────────────────────────────────

async def test_correct_source_optional_and_never_fabricated(bearer):
    """source 是声明性自述；缺失不拒（上游无承载字段），提供则原样透传不编造。"""
    fake = FakeClient(results=[{"correction_id": 7}] * 2)
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_correct", {
            "dataset_id": "ds1", "op": "update",
            "row_id": "1", "column": "地块编号", "value": "A9"})
        await c.call_tool("wire_dataset_correct", {
            "dataset_id": "ds1", "op": "update", "source": "conversation:9",
            "row_id": "2", "column": "地块编号", "value": "A8"})
    first = fake.calls[0]["json_body"]
    second = fake.calls[1]["json_body"]
    assert "source" not in first  # 缺 source 不代填（不编造）
    assert second["source"] == "conversation:9"  # 提供则透传


async def test_correct_blank_source_treated_as_absent(bearer):
    """空白 source 按无 source 处理（不把空白塞进载荷、不报错）。"""
    fake = FakeClient(results=[{"correction_id": 7}])
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_correct", {
            "dataset_id": "ds1", "op": "delete", "row_id": "1", "source": "   "})
    assert len(fake.calls) == 1
    assert "source" not in fake.calls[0]["json_body"]


async def test_correct_rejects_unknown_op(bearer):
    fake = FakeClient()
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_dataset_correct", {
                "dataset_id": "ds1", "op": "upsert", "source": "conversation:1"})
    assert "insert、update 或 delete" in str(ei.value)


async def test_correct_forwards_all_branches(bearer):
    fake = FakeClient(results=[{"correction_id": 7}] * 3)
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_correct", {
            "dataset_id": "ds1", "op": "update", "source": "conversation:1",
            "row_id": "1", "column": "地块编号", "value": "A9"})
        await c.call_tool("wire_dataset_correct", {
            "dataset_id": "ds1", "op": "delete", "source": "conversation:1", "row_id": "1"})
        await c.call_tool("wire_dataset_correct", {
            "dataset_id": "ds1", "op": "insert", "source": "conversation:1",
            "values": {"地块编号": "A1", "记录日期": "2026-10-16"}, "artifact_id": 11})
    ops = [call["json_body"]["op"] for call in fake.calls]
    assert ops == ["update", "delete", "insert"]
    assert fake.calls[0]["json_body"]["value"] == "A9"
    assert fake.calls[1]["json_body"]["row_id"] == "1"
    assert fake.calls[2]["json_body"]["values"]["地块编号"] == "A1"
    assert fake.calls[2]["json_body"]["artifact_id"] == 11
    assert all(call["method"] == "POST" for call in fake.calls)
    assert all(call["path"] == "/api/v1/datasets/ds1/corrections" for call in fake.calls)


# ── 历史 / 撤销 ──────────────────────────────────────────────────────────────

async def test_corrections_list_defaults_include_all(bearer):
    fake = FakeClient(by_path={"GET /api/v1/datasets/ds1/corrections": {"corrections": []}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_corrections_list", {"dataset_id": "ds1"})
    assert fake.calls[0]["params"] == {"include_reverted": True, "include_dangling": True}


async def test_corrections_list_explicit_exclude(bearer):
    fake = FakeClient(by_path={"GET /api/v1/datasets/ds1/corrections": {"corrections": []}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        await c.call_tool("wire_dataset_corrections_list", {
            "dataset_id": "ds1", "include_reverted": False, "include_dangling": False})
    assert fake.calls[0]["params"] == {"include_reverted": False, "include_dangling": False}


async def test_revert_endpoint_shape(bearer):
    fake = FakeClient(by_path={
        "POST /api/v1/datasets/ds1/corrections/7/revert": {"reverted": True}})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        out = await c.call_tool("wire_dataset_correction_revert", {
            "dataset_id": "ds1", "correction_id": 7})
    data = out.data if hasattr(out, "data") else out
    assert data["reverted"] is True
    assert fake.calls[0]["path"] == "/api/v1/datasets/ds1/corrections/7/revert"


async def test_revert_409_passthrough(bearer):
    """重复撤销由上游判 409——工具层原样透传，不谎报成功。"""
    fake = FakeClient(by_path={
        "POST /api/v1/datasets/ds1/corrections/7/revert": WireError(
            "conflict", 409, "修正 7 已回滚")})
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_dataset_correction_revert", {
                "dataset_id": "ds1", "correction_id": 7})
    assert "409" in str(ei.value) or "conflict" in str(ei.value).lower()


# ── 鉴权门 ──────────────────────────────────────────────────────────────────

async def test_missing_credential_never_touches_upstream(monkeypatch):
    monkeypatch.setattr(ts, "_current_bearer", lambda: None)
    fake = FakeClient()
    mcp = await _mk(fake)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("wire_dataset_rows_read", {"dataset_id": "ds1"})
    assert "wdk_" in str(ei.value)
    assert fake.calls == []
