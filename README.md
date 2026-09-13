# tini_projects

单机 MVP 后端：**FastAPI + SQLite + Mock Agent + 显式 Scheduler**（`fair-kind / cap=2`）。不调用真实 LLM。

前端静态页是**独立 PR**，本分支不包含 UI。

## 运行 API

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m backend
# 等价：uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

只绑定 `127.0.0.1`。无鉴权。数据目录默认 `./data/`（已 gitignore）。

### 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `TINI_DATA_DIR` | `data` | SQLite 与 Context 根目录 |
| `TINI_MAX_RUNNING` | `2` | 每项目并发帽 |
| `TINI_DURATION_RESEARCH` | `4` | Mock research 睡眠秒数 |
| `TINI_DURATION_IMPLEMENT` | `10` | implement |
| `TINI_DURATION_TEST` | `6` | test |
| `TINI_DURATION_GENERIC` | `8` | generic |
| `TINI_AUTO_SUMMARY` | `1` | Worker 全部结束后自动汇总并更新 `notes.md` |
| `TINI_HOST` | `127.0.0.1` | 监听地址 |
| `TINI_PORT` | `8000` | 端口 |

## 运行测试

```bash
pip install -r requirements.txt
pytest -q
```

## 调度演示（API）

1. `POST /api/projects` `{"name":"demo","goal":"..."}`
2. `POST /api/projects/{id}/messages` `{"content":"/demo-schedule"}`
3. `GET /api/projects/{id}/scheduler` 应立刻为 **2 running + 2 queued**（research/implement 运行，test/generic 排队）。
4. 公平性对照：`/demo-fairness`（3×research + test）。

Coordinator **只入队**；`asyncio.create_task` 只出现在 `WorkerManager.start`，且仅当 Scheduler 放行之后。

## API（前缀 `/api`）

错误体一律 `{ "error": "..." }`。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/projects` | 建 Project，初始化 Context 骨架 |
| GET | `/projects` | 列表（含未完成 Worker 数） |
| GET | `/projects/{id}` | 详情 |
| PATCH | `/projects/{id}` | 改 name/goal/status |
| GET | `/projects/{id}/messages` | `?after=` 分页 |
| POST | `/projects/{id}/messages` | 用户发言，跑 Mock Coordinator |
| GET | `/projects/{id}/workers` | Worker 列表 |
| GET | `/projects/{id}/workers/{wid}` | 详情 + 报告路径 |
| GET | `/projects/{id}/scheduler` | 调度快照 |
| POST | `/projects/{id}/workers/{wid}/cancel` | 取消后 `kick()` |
| GET | `/projects/{id}/context` | 目录树 |
| GET | `/projects/{id}/context/file?path=` | 文本 JSON 或二进制下载 |
| PUT | `/projects/{id}/context/file?path=` | 手改文本 |
| POST | `/projects/{id}/attachments` | `multipart` 字段 `file` |
| POST | `/projects/{id}/attached-chats` | `{"content"}` 导入 transcript |
| GET | `/projects/{id}/events` | SSE |

`GET /scheduler` 形状：

```json
{
  "policy": "fair-kind",
  "max_running": 2,
  "running": [{ "id": "...", "kind": "research", "schedule_reason": "fifo", "started_at": "..." }],
  "queued": [{ "id": "...", "kind": "test", "priority": "normal", "enqueue_seq": 3, "position": 1 }]
}
```
