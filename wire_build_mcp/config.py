"""环境配置（家族模式：Settings.from_env，测试可注入）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://wire.finddatatech.cloud"
    timeout_seconds: float = 20.0
    auth_cache_ttl_seconds: float = 60.0

    @classmethod
    def from_env(cls, env=None) -> Settings:
        env = env if env is not None else os.environ
        return cls(
            base_url=(env.get("WIRE_BUILD_BASE_URL") or cls.base_url).rstrip("/"),
            timeout_seconds=float(env.get("WIRE_BUILD_TIMEOUT_SECONDS") or 20.0),
            auth_cache_ttl_seconds=float(env.get("WIRE_BUILD_AUTH_CACHE_TTL") or 60.0),
        )
