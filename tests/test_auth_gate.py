"""AuthGate（家族变体）：无 Bearer 拒、有 Bearer 放行。"""

from __future__ import annotations

from wire_build_mcp.auth_gate import AuthGate


async def test_anonymous_rejected():
    assert await AuthGate().check(None) is False


async def test_any_bearer_passes():
    assert await AuthGate().check("anything") is True
