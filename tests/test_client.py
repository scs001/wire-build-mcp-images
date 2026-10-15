"""client 契约测试：Bearer 透传 + wire 错误映射 + multipart 上传。"""

from __future__ import annotations

import httpx
import pytest

from wire_build_mcp.client import MISSING_CREDENTIAL_HINT, WireClient, WireError


def make_client(handler) -> WireClient:
    return WireClient("https://wire.example", timeout_seconds=5,
                      transport=httpx.MockTransport(handler))


async def test_bearer_forwarded_verbatim():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    await client.request("GET", "/api/v1/artifacts", bearer="id-token-or-wdk")
    assert seen["auth"] == "Bearer id-token-or-wdk"


async def test_upload_file_multipart():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["ctype"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(201, json={"upload_id": "u1", "columns": []})

    client = make_client(handler)
    out = await client.upload_file(
        "/api/v1/datasets/uploads", bearer="tok",
        filename="ds.csv", content=b"a,b\n1,2\n",
    )
    assert out["upload_id"] == "u1"
    assert seen["auth"] == "Bearer tok"
    assert "multipart/form-data" in seen["ctype"]
    assert b"ds.csv" in seen["body"] and b"a,b" in seen["body"]


async def test_upload_missing_credential_never_touches_upstream():
    def handler(request):
        raise AssertionError("无凭据不应触上游")

    client = make_client(handler)
    with pytest.raises(WireError) as ei:
        await client.upload_file("/x", bearer=None, filename="f.csv", content=b"a\n")
    assert ei.value.kind == "missing_credential"


async def test_missing_credential_never_touches_upstream():
    def handler(request):
        raise AssertionError("无凭据不应触上游")

    client = make_client(handler)
    with pytest.raises(WireError) as ei:
        await client.request("GET", "/api/v1/artifacts", bearer=None)
    assert ei.value.kind == "missing_credential"
    assert MISSING_CREDENTIAL_HINT in ei.value.detail


@pytest.mark.parametrize(
    "status,kind",
    [(401, "unauthorized"), (402, "billing"), (403, "forbidden"), (404, "not_found"),
     (409, "conflict"), (422, "invalid"), (429, "rate_limited"), (500, "unreachable")],
)
async def test_status_mapping(status, kind):
    def handler(request):
        return httpx.Response(status, json={"detail": f"boom {status}"})

    client = make_client(handler)
    with pytest.raises(WireError) as ei:
        await client.request("GET", "/x", bearer="tok")
    assert ei.value.kind == kind
    assert f"boom {status}" in ei.value.detail


async def test_transport_error_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    with pytest.raises(WireError) as ei:
        await client.request("GET", "/x", bearer="tok")
    assert ei.value.kind == "unreachable"
    assert "不可达" in ei.value.detail
