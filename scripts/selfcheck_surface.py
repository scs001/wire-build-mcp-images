#!/usr/bin/env python3
"""tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

家族配方（同 wire-ops-mcp/scripts/selfcheck_surface.py），仅 import 包名不同。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tool-surface.json"


def fail(problems):
    print("SELFCHECK FAIL:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)


def main() -> None:
    if not MANIFEST.exists():
        fail([f"manifest 缺失：{MANIFEST}（契约：缺失即未上牌）"])
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    levels = {}
    for g in m.get("tool_groups", []):
        level = g.get("level")
        if level not in ("commercial", "internal"):
            fail([f"tool_groups.{g.get('group')} 的 level 非法：{level!r}"])
        for t in g.get("tools", []):
            if t in levels and levels[t] != level:
                fail([f"工具 {t} 在多个 group 中级别冲突"])
            levels[t] = level

    async def runtime():
        sys.path.insert(0, str(REPO))
        from wire_build_mcp.server import create_server

        return {t.name for t in await create_server().list_tools()}

    declared = set(levels)
    runtime_set = asyncio.run(runtime())
    problems = []
    if declared - runtime_set:
        problems.append(f"manifest 多声明（代码未注册）：{sorted(declared - runtime_set)}")
    if runtime_set - declared:
        problems.append(f"代码多注册（manifest 未声明）：{sorted(runtime_set - declared)}")
    if problems:
        fail(problems)
    print(f"SELFCHECK PASS: {len(runtime_set)} tools, manifest↔runtime 一致")


if __name__ == "__main__":
    main()
