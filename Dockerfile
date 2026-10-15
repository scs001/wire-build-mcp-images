# wire-build-mcp image — 柏讯（wire 线）产物构建 MCP。
#
# 发布通道：GitHub Actions -> TCR（scs001 公开仓免费额度线，家族配方）。
FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY wire_build_mcp ./wire_build_mcp

RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" .; else pip install --no-cache-dir .; fi \
 && useradd -m -u 1000 appuser

USER appuser

EXPOSE 8000

CMD ["wire-build-mcp", "serve", "--host", "0.0.0.0", "--port", "8000"]
