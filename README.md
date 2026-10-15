# wire-build-mcp — 柏讯产物构建 MCP

柏讯（wire 线）的**产物构建面** MCP：让 AI agent 能按 spec 创建/更新/发布数据录入
表单（form）与单记录扫码页（record-view），并取数据集列事实、按需建集。**草稿优先的
写入纪律**：创建/更新一律落 draft，发布必须显式单独调用。

- 契约：`openspec/changes/wire-ai-artifact-build/specs/wire-build-mcp/spec.md`
  （finddata 工作区；后续治理面见 `wire-ai-data-steward`）
- 接入规约：`LINES-MCP-CONFORMANCE.md`

## 与 wire-ops-mcp 的分工

| 入口 | 定位 | 工具面 |
|---|---|---|
| `wire-ops-mcp` | 客户面**读**操作 | 需求单 / 数据集只读查询 / 市场订阅 |
| `wire-build-mcp` | 产物**构建**（写） | 产物 CRUD + 列事实 + 建集 |

两入口并列，各自独立部署与凭据；`wire-ops-mcp` 十工具面不受本服务影响。

## 工具面

| 组 | 工具 | 写 |
|---|---|---|
| 事实 | `wire_dataset_schema`（列名+类型）、`wire_artifact_list`、`wire_artifact_get` | 全读 |
| 建集 | `wire_dataset_create`（CSV 内容 → 导入建集） | ✅ |
| 产物写 | `wire_artifact_form_create`、`wire_artifact_record_view_create`、`wire_artifact_update` | ✅（落草稿） |
| 状态 | `wire_artifact_publish`、`wire_artifact_archive`、`wire_artifact_restore` | ✅ |

## 两条硬纪律

1. **写入落草稿，发布独立**。上游 `publish` 端点自身不做产物结构校验（仅状态迁移），
   因此本服务**不提供**「创建即发布」的开关（多余参数会被 MCP 层直接拒绝）。
   发布前请人工在控制台目视一次，或确认 spec 已过校验。
2. **列名核对**。spec 引用了数据集中不存在的列时，错误响应会**带上该数据集的真实
   列名清单**——生成 spec 前请先用 `wire_dataset_schema` 取事实，别编列名。

## 鉴权

进站 Bearer **原样转发**，wire 侧判轨（Logto ID token console 轨 / `wdk_` 数据面 key）:
- 无凭据：本服务 401，不触上游
- 坏凭据：上游 401 原文透传
- 跨企业产物/数据集：404 与「不存在」不可区分

## 环境

| 变量 | 默认 |
|---|---|
| `WIRE_BUILD_BASE_URL` | `https://wire.finddatatech.cloud` |
| `WIRE_BUILD_TIMEOUT_SECONDS` | `20` |
| `WIRE_BUILD_AUTH_CACHE_TTL` | `60` |
