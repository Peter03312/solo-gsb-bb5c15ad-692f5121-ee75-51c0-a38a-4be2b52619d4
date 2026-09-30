# 双盲论文评审分配服务

基于 **Python + FastAPI + SQLite** 的双盲评审分配服务。会务方凭本地密钥维护论文与评审人资料，
求解器在硬约束下自动计算全局最优分配方案，支持预演、版本化发布与评审人匿名稿分发。

## 功能与规则

**分配硬约束**（每篇论文恰好 2 名评审人）：

1. 两名评审人必须来自**不同机构**；
2. 两人中**至少一人擅长该论文主题**（擅长主题与论文主题交集非空；论文未标注主题时视为均满足）；
3. 评审人**不得与任一作者同机构**；
4. 评审人**不得评审自己声明回避的论文**；
5. 评审人已分配论文数**不得超过其容量**。

**优化目标**（多解时按优先级）：

1. **最小化最高已用容量比例**（全体评审人中 `已分配数 / 容量` 的最大值）；
2. 在此前提下，按 **(论文编号, 评审人编号) 展开的全局序列取字典序最小**的方案。

**无完整方案时**：返回最大部分分配、未分配论文清单及每篇的限制原因（合格评审人不足、
容量占满、无擅长者、机构冲突等），**已发布版本保持不变**。

**版本化发布**：每次资料变更（论文/评审人增删改）使资料修订号 +1；预演返回所依据的修订号，
发布必须携带该修订号，不一致则 `409` 拒绝。校验修订号、重算方案、写入发布版在**同一个
SQLite 写事务**（`BEGIN IMMEDIATE`）内完成，与资料修改并发不会产生基于旧版资料的方案。

**权限模型**：

| 角色 | 凭据 | 权限 |
|---|---|---|
| 会务方 | 本地密钥（请求头 `X-Organizer-Key`） | 维护资料、预演、发布、查询分配与排除原因 |
| 评审人 | 自身凭据（`X-Reviewer-Id` + `X-Reviewer-Credential`） | 仅可读取**已发布方案中分配给自己**的匿名稿（不含作者机构） |

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址：**http://localhost:8000**（交互式 API 文档：http://localhost:8000/docs）

SQLite 数据库文件在首次启动时自动初始化，持久化于名为 `review_data` 的卷（容器内 `/data/app.db`）。

### 仅 Docker

```bash
docker build -t review-assignment .
docker run -d -p 8000:8000 \
  -e ORGANIZER_KEY=please-change-me \
  -v review_data:/data \
  review-assignment
```

### 本地开发（无 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ORGANIZER_KEY=dev-organizer-key python -m app.main
```

## 配置

均通过环境变量配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ORGANIZER_KEY` | `dev-organizer-key` | 会务方本地密钥，**生产环境必须修改** |
| `DB_PATH` | `./data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

Compose 下可用 `ORGANIZER_KEY=xxx PORT=9000 docker compose up -d` 覆盖。

## API 一览

会务方接口均需请求头 `X-Organizer-Key`；评审人接口需 `X-Reviewer-Id` 与 `X-Reviewer-Credential`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（无需凭据） |
| GET | `/meta` | 当前资料修订号与发布状态 |
| POST | `/papers` | 录入论文（编号/匿名稿/主题/作者机构），重复编号 `409` |
| PUT / DELETE | `/papers/{paper_id}` | 更新 / 删除论文，不存在 `404` |
| GET | `/papers` | 论文列表 |
| POST | `/reviewers` | 录入评审人（编号/凭据/擅长主题/机构/容量/回避论文），重复编号 `409`，非法容量 `422` |
| PUT / DELETE | `/reviewers/{reviewer_id}` | 更新 / 删除评审人 |
| GET | `/reviewers` | 评审人列表 |
| POST | `/assignment/dry-run` | 预演：返回方案、未分配论文与限制原因、资料修订号；不影响已发布版 |
| POST | `/assignment/publish` | 发布：携带 `{"base_revision": N}`，修订号过期 `409`，无可行方案 `422` |
| GET | `/assignment` | 查询已发布方案及每篇论文的分配/排除原因 |
| GET | `/reviewer/assignments` | 评审人读取分配给自己的匿名稿（凭据失效 `401`） |

## 调用示例

完整可运行脚本见 [`examples/demo.sh`](examples/demo.sh)（服务启动后执行 `./examples/demo.sh`）。

```bash
BASE=http://localhost:8000
ORG='X-Organizer-Key: dev-organizer-key'

# 1. 录入评审人
curl -X POST $BASE/reviewers -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R1", "credential": "r1-secret", "topics": ["AI"],
  "institution": "Inst-A", "capacity": 2, "avoid_papers": []
}'

# 2. 录入论文
curl -X POST $BASE/papers -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]
}'

# 3. 预演（记下返回的 revision）
curl -X POST $BASE/assignment/dry-run -H "$ORG"

# 4. 发布（携带预演所用修订号；资料已变动则返回 409，需重新预演）
curl -X POST $BASE/assignment/publish -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"base_revision": 2}'

# 5. 会务方查询分配与排除原因
curl $BASE/assignment -H "$ORG"

# 6. 评审人凭自身凭据读取已分配匿名稿
curl $BASE/reviewer/assignments -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
```

预演响应示例（可行）：

```json
{
  "feasible": true,
  "revision": 7,
  "plan": {"P1": ["R1", "R2"]},
  "unassigned": [],
  "max_used_capacity_ratio": "3/4",
  "papers": {"P1": {"assigned": ["R1", "R2"],
    "eligible_reviewers": ["R1", "R2", "R3"],
    "excluded": [{"reviewer_id": "R4", "reason": "same_institution_as_author"}]}},
  "diagnostics": {}
}
```

预演响应示例（无完整方案，`papers` 为最大部分分配，`diagnostics` 给出每篇未分配论文的限制原因）：

```json
{
  "feasible": false,
  "unassigned": ["P2"],
  "diagnostics": {"P2": {
    "eligible_reviewers": ["R3"],
    "eligible_at_capacity": ["R3"],
    "excluded": [{"reviewer_id": "R1", "reason": "same_institution_as_author"}],
    "reasons": ["fewer_than_two_eligible_reviewers", "eligible_reviewers_at_capacity"]
  }}
}
```

## 错误码

| 状态码 | 场景 |
|---|---|
| 401 | 会务方密钥缺失/错误；评审人凭据缺失/失效 |
| 404 | 更新/删除不存在的论文或评审人；尚未发布方案 |
| 409 | 论文/评审人编号重复；发布携带的修订号已过期（资料已变更） |
| 422 | 非法容量（非整数、小于 1）、缺字段等参数校验失败；无可行方案时强行发布 |

排除/限制原因代码：`same_institution_as_author`（与作者同机构）、`on_reviewer_avoid_list`
（在回避名单）、`fewer_than_two_eligible_reviewers`（合格者不足两人）、
`eligible_reviewers_at_capacity`（合格者容量占满）、`no_expert_among_eligible_reviewers`
（合格者无人擅长主题）、`eligible_reviewers_share_single_institution`（合格者同属一个机构）、
`no_valid_reviewer_pair`（不存在合法评审人对）、`all_valid_pairs_blocked_by_capacity`
（所有合法评审人对均被容量阻塞）。

## 项目结构

```
app/
  main.py     # FastAPI 路由、鉴权、发布事务
  solver.py   # 分配求解器（约束检查、最小化最高容量比例、字典序最小、部分方案与诊断）
  db.py       # SQLite 连接、schema 初始化、修订号、读写事务
  schemas.py  # 请求体模型与校验
tests/test_api.py   # 24 个接口与求解器测试（含并发发布测试）
examples/demo.sh    # 端到端 curl 示例
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```
