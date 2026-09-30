"""双盲论文评审分配服务 - HTTP API。

角色:
  - 会务方: 凭本地密钥 (请求头 X-Organizer-Key) 维护论文/评审人资料, 预演并发布分配方案,
    并在发布后基于评审人决定预演/执行补位;
  - 评审人: 凭自身凭据 (X-Reviewer-Id + X-Reviewer-Credential) 仅可读取已分配给自己的匿名稿,
    并对当前发布方案中本人的分配提交确认或回避决定。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    DecisionIn,
    PaperIn,
    PaperUpdate,
    PublishIn,
    ReassignPublishIn,
    ReviewerIn,
    ReviewerUpdate,
)
from .solver import Paper, Reviewer, compute_assignment

ORGANIZER_KEY = os.environ.get("ORGANIZER_KEY", "dev-organizer-key")

app = FastAPI(title="双盲论文评审分配服务", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "detail": "请求参数校验失败 (例如容量不是 >=1 的整数、缺少必填字段)",
            "errors": jsonable_encoder(exc.errors()),
        },
    )


# ---------------------------------------------------------------- 鉴权

def require_organizer(x_organizer_key: str = Header(default=None)) -> bool:
    if not x_organizer_key or x_organizer_key != ORGANIZER_KEY:
        raise HTTPException(status_code=401, detail="会务方密钥无效 (invalid organizer key)")
    return True


def require_reviewer(
    x_reviewer_id: str = Header(default=None),
    x_reviewer_credential: str = Header(default=None),
) -> str:
    if not x_reviewer_id or not x_reviewer_credential:
        raise HTTPException(status_code=401, detail="缺少评审人编号或凭据 (missing reviewer credentials)")
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT credential FROM reviewers WHERE reviewer_id = ?", (x_reviewer_id,)
        ).fetchone()
    if row is None or row["credential"] != x_reviewer_credential:
        raise HTTPException(status_code=401, detail="评审人凭据无效 (invalid reviewer credentials)")
    return x_reviewer_id


# ---------------------------------------------------------------- 工具

def _load_domain(conn):
    papers = [
        Paper(
            paper_id=r["paper_id"],
            topics=frozenset(json.loads(r["topics"])),
            institutions=frozenset(json.loads(r["institutions"])),
        )
        for r in conn.execute("SELECT * FROM papers").fetchall()
    ]
    # 评审人已回避的 (论文, 评审人) 对构成后续分配的硬回避
    declined = {}
    for d in conn.execute(
        "SELECT paper_id, reviewer_id FROM decisions WHERE decision = 'declined'"
    ).fetchall():
        declined.setdefault(d["reviewer_id"], set()).add(d["paper_id"])
    reviewers = [
        Reviewer(
            reviewer_id=r["reviewer_id"],
            topics=frozenset(json.loads(r["topics"])),
            institution=r["institution"],
            capacity=r["capacity"],
            avoid_papers=frozenset(json.loads(r["avoid_papers"])),
            declined_papers=frozenset(declined.get(r["reviewer_id"], ())),
        )
        for r in conn.execute("SELECT * FROM reviewers").fetchall()
    ]
    return papers, reviewers


def _paper_row_to_dict(row):
    return {
        "paper_id": row["paper_id"],
        "manuscript": row["manuscript"],
        "topics": json.loads(row["topics"]),
        "institutions": json.loads(row["institutions"]),
    }


def _reviewer_row_to_dict(row):
    return {
        "reviewer_id": row["reviewer_id"],
        "credential": row["credential"],
        "topics": json.loads(row["topics"]),
        "institution": row["institution"],
        "capacity": row["capacity"],
        "avoid_papers": json.loads(row["avoid_papers"]),
    }


def _get_published(conn):
    """当前发布版行; 尚未发布返回 None。"""
    return conn.execute("SELECT * FROM published WHERE id = 1").fetchone()


def _write_published(conn, revision, result):
    """写入新发布版 (发布版序号单调 +1)。"""
    now = datetime.now(timezone.utc).isoformat()
    prev = _get_published(conn)
    version = (prev["version"] + 1) if prev else 1
    conn.execute(
        "INSERT INTO published(id, revision, version, plan, explanations, published_at)"
        " VALUES (1,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET revision = excluded.revision,"
        " version = excluded.version, plan = excluded.plan,"
        " explanations = excluded.explanations, published_at = excluded.published_at",
        (
            revision,
            version,
            json.dumps(result["plan"], ensure_ascii=False),
            json.dumps(result["papers"], ensure_ascii=False),
            now,
        ),
    )
    return version


def _decisions_for_plan(conn, plan):
    """当前发布方案内各 (论文, 评审人) 的决定状态, 供会务方核对。"""
    out = {}
    for pid, rids in plan.items():
        for rid in rids:
            row = conn.execute(
                "SELECT decision, reason FROM decisions WHERE paper_id = ? AND reviewer_id = ?",
                (pid, rid),
            ).fetchone()
            if row is not None:
                out.setdefault(pid, {})[rid] = {"decision": row["decision"], "reason": row["reason"]}
    return out


def _declined_papers_of(conn, reviewer_id):
    return {
        r["paper_id"]
        for r in conn.execute(
            "SELECT paper_id FROM decisions WHERE reviewer_id = ? AND decision = 'declined'",
            (reviewer_id,),
        ).fetchall()
    }


# ---------------------------------------------------------------- 基础

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/meta")
def meta(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        published = _get_published(conn)
    return {
        "revision": rev,
        "published": (
            {
                "revision": published["revision"],
                "published_version": published["version"],
                "published_at": published["published_at"],
            }
            if published
            else None
        ),
    }


# ---------------------------------------------------------------- 论文资料 (会务方)

@app.post("/papers", status_code=201)
def create_paper(body: PaperIn, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (body.paper_id,)).fetchone():
            raise HTTPException(status_code=409, detail=f"论文编号重复: {body.paper_id}")
        conn.execute(
            "INSERT INTO papers(paper_id, manuscript, topics, institutions) VALUES (?,?,?,?)",
            (body.paper_id, body.manuscript, json.dumps(body.topics), json.dumps(body.institutions)),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": body.paper_id, "revision": rev}


@app.put("/papers/{paper_id}")
def update_paper(paper_id: str, body: PaperUpdate, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if not conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"论文不存在: {paper_id}")
        conn.execute(
            "UPDATE papers SET manuscript = ?, topics = ?, institutions = ? WHERE paper_id = ?",
            (body.manuscript, json.dumps(body.topics), json.dumps(body.institutions), paper_id),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": paper_id, "revision": rev}


@app.delete("/papers/{paper_id}")
def delete_paper(paper_id: str, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        cur = conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"论文不存在: {paper_id}")
        conn.execute("DELETE FROM decisions WHERE paper_id = ?", (paper_id,))
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": paper_id, "revision": rev}


@app.get("/papers")
def list_papers(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rows = conn.execute("SELECT * FROM papers ORDER BY paper_id").fetchall()
    return {"papers": [_paper_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------- 评审人资料 (会务方)

@app.post("/reviewers", status_code=201)
def create_reviewer(body: ReviewerIn, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if conn.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (body.reviewer_id,)
        ).fetchone():
            raise HTTPException(status_code=409, detail=f"评审人编号重复: {body.reviewer_id}")
        conn.execute(
            "INSERT INTO reviewers(reviewer_id, credential, topics, institution, capacity, avoid_papers)"
            " VALUES (?,?,?,?,?,?)",
            (
                body.reviewer_id,
                body.credential,
                json.dumps(body.topics),
                body.institution,
                body.capacity,
                json.dumps(body.avoid_papers),
            ),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": body.reviewer_id, "revision": rev}


@app.put("/reviewers/{reviewer_id}")
def update_reviewer(reviewer_id: str, body: ReviewerUpdate, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if not conn.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (reviewer_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        conn.execute(
            "UPDATE reviewers SET credential = ?, topics = ?, institution = ?, capacity = ?,"
            " avoid_papers = ? WHERE reviewer_id = ?",
            (
                body.credential,
                json.dumps(body.topics),
                body.institution,
                body.capacity,
                json.dumps(body.avoid_papers),
                reviewer_id,
            ),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": reviewer_id, "revision": rev}


@app.delete("/reviewers/{reviewer_id}")
def delete_reviewer(reviewer_id: str, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        cur = conn.execute("DELETE FROM reviewers WHERE reviewer_id = ?", (reviewer_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        conn.execute("DELETE FROM decisions WHERE reviewer_id = ?", (reviewer_id,))
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": reviewer_id, "revision": rev}


@app.get("/reviewers")
def list_reviewers(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rows = conn.execute("SELECT * FROM reviewers ORDER BY reviewer_id").fetchall()
    return {"reviewers": [_reviewer_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------- 预演 / 发布 (会务方)

@app.post("/assignment/dry-run")
def dry_run(_: bool = Depends(require_organizer)):
    """基于当前资料试算分配方案; 不改动已发布版本。返回所依据的资料修订号。"""
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        papers, reviewers = _load_domain(conn)
    result = compute_assignment(papers, reviewers)
    result["revision"] = rev
    return result


@app.post("/assignment/publish")
def publish(body: PublishIn, _: bool = Depends(require_organizer)):
    """发布分配方案。必须携带预演所用资料修订号; 资料已变动则拒绝 (409)。

    校验修订号、重算方案、写入发布版在同一个写事务内完成,
    与资料修改并发时不会产生基于旧版资料的方案。
    """
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        if body.base_revision != rev:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"资料已变更: 预演基于修订号 {body.base_revision}, "
                    f"当前修订号为 {rev}, 请重新预演后再发布"
                ),
            )
        papers, reviewers = _load_domain(conn)
        result = compute_assignment(papers, reviewers)
        if not result["feasible"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前资料下不存在完整分配方案, 无法发布",
                    "unassigned": result["unassigned"],
                    "diagnostics": result["diagnostics"],
                },
            )
        version = _write_published(conn, rev, result)
    return {"ok": True, "revision": rev, "published_version": version, "plan": result["plan"]}


@app.get("/assignment")
def get_published(_: bool = Depends(require_organizer)):
    """会务方查询当前已发布方案、每篇论文的分配/排除原因及评审人决定状态。"""
    with db.read_txn() as conn:
        row = _get_published(conn)
        if row is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案")
        plan = json.loads(row["plan"])
        decisions = _decisions_for_plan(conn, plan)
    return {
        "revision": row["revision"],
        "published_version": row["version"],
        "published_at": row["published_at"],
        "plan": plan,
        "papers": json.loads(row["explanations"]),
        "decisions": decisions,
    }


# ---------------------------------------------------------------- 补位 (会务方)

def _compute_reassignment(conn):
    """基于当前发布版与最新资料计算补位方案 (补位预演与补位发布共用)。

    已确认且未回避的 (论文, 评审人) 关系固定; 已回避关系成为硬回避;
    其余位置遵守原有硬约束与优化次序。返回 (发布版行, 固定关系, 求解结果)。
    """
    pub = _get_published(conn)
    if pub is None:
        raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无法补位")
    plan = json.loads(pub["plan"])
    confirmed = {
        (d["paper_id"], d["reviewer_id"])
        for d in conn.execute(
            "SELECT paper_id, reviewer_id FROM decisions WHERE decision = 'confirmed'"
        ).fetchall()
    }
    pinned = {}
    for pid, rids in plan.items():
        members = tuple(sorted(r for r in rids if (pid, r) in confirmed))
        if members:
            pinned[pid] = members
    papers, reviewers = _load_domain(conn)
    result = compute_assignment(papers, reviewers, pinned=pinned)
    return pub, pinned, result


@app.post("/assignment/reassign/dry-run")
def reassign_dry_run(_: bool = Depends(require_organizer)):
    """补位预演: 以当前发布版及资料修订号试算补位方案; 不改动已发布版本。

    返回方案、固定关系、未补齐论文与限制原因, 以及所依据的资料修订号 (revision)
    与发布版序号 (published_version), 供补位发布复核。
    """
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        pub, pinned, result = _compute_reassignment(conn)
        result["revision"] = rev
        result["published_version"] = pub["version"]
        result["pinned"] = {pid: list(m) for pid, m in sorted(pinned.items())}
    return result


@app.post("/assignment/reassign/publish")
def reassign_publish(body: ReassignPublishIn, _: bool = Depends(require_organizer)):
    """补位发布。须同时携带补位预演返回的资料修订号与发布版序号, 任一过期 (409) 拒绝。

    校验两种版本、重算补位方案、写入新发布版在同一个写事务内完成。
    成功后仅按新发布版授权取稿; 仍在方案中的确认状态予以保留。
    """
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        pub, pinned, result = _compute_reassignment(conn)
        if body.base_revision != rev or body.base_published_version != pub["version"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"版本已过期: 预演基于 (资料修订号 {body.base_revision}, 发布版序号 "
                    f"{body.base_published_version}), 当前为 ({rev}, {pub['version']}), "
                    f"请重新补位预演后再发布"
                ),
            )
        if not result["feasible"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前资料与已确认关系下不存在完整补位方案, 无法发布",
                    "unassigned": result["unassigned"],
                    "diagnostics": result["diagnostics"],
                },
            )
        version = _write_published(conn, rev, result)
    return {
        "ok": True,
        "revision": rev,
        "published_version": version,
        "pinned": {pid: list(m) for pid, m in sorted(pinned.items())},
        "plan": result["plan"],
    }


# ---------------------------------------------------------------- 评审人视图

@app.get("/reviewer/assignments")
def reviewer_assignments(reviewer_id: str = Depends(require_reviewer)):
    """评审人凭自身凭据读取已分配给自己的匿名稿 (仅已发布方案, 不含作者机构)。

    已回避的论文立即从本人视图中移除 (当前发布方案对会务方仍保持原样)。
    """
    with db.read_txn() as conn:
        row = conn.execute("SELECT plan FROM published WHERE id = 1").fetchone()
        if row is None:
            return {"reviewer_id": reviewer_id, "assignments": []}
        plan = json.loads(row["plan"])
        declined = _declined_papers_of(conn, reviewer_id)
        mine = sorted(
            pid for pid, rs in plan.items() if reviewer_id in rs and pid not in declined
        )
        out = []
        for pid in mine:
            paper = conn.execute(
                "SELECT paper_id, topics, manuscript FROM papers WHERE paper_id = ?", (pid,)
            ).fetchone()
            if paper is not None:
                out.append(
                    {
                        "paper_id": paper["paper_id"],
                        "topics": json.loads(paper["topics"]),
                        "manuscript": paper["manuscript"],
                    }
                )
    return {"reviewer_id": reviewer_id, "assignments": out}


# ---------------------------------------------------------------- 评审人决定

@app.post("/reviewer/decisions")
def submit_decision(body: DecisionIn, reviewer_id: str = Depends(require_reviewer)):
    """对当前发布方案中本人的分配提交决定: 确认 (confirm) 或回避 (decline, 须非空原因)。

    - 同一决定重复提交幂等 (返回当前状态, 不再推进修订号; 回避以首次原因为准);
    - 已回避任务不能再确认 (409); 未在发布方案中的分配拒绝 (404);
    - 有效决定 (状态发生变化) 推进资料修订号;
    - 回避立即停止本人读取该匿名稿, 并成为后续分配的硬回避。
    """
    with db.write_txn() as conn:
        pub = _get_published(conn)
        if pub is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案")
        plan = json.loads(pub["plan"])
        assigned = plan.get(body.paper_id)
        if not assigned or reviewer_id not in assigned:
            raise HTTPException(
                status_code=404,
                detail=f"当前发布方案中不存在该分配: ({body.paper_id}, {reviewer_id})",
            )
        new_state = "confirmed" if body.decision == "confirm" else "declined"
        existing = conn.execute(
            "SELECT decision, reason FROM decisions WHERE paper_id = ? AND reviewer_id = ?",
            (body.paper_id, reviewer_id),
        ).fetchone()
        if existing is not None and existing["decision"] == new_state:
            # 同一决定重复提交: 幂等, 不推进修订号
            return {
                "ok": True,
                "paper_id": body.paper_id,
                "reviewer_id": reviewer_id,
                "decision": new_state,
                "reason": existing["reason"],
                "changed": False,
                "revision": db.get_revision(conn),
            }
        if existing is not None and existing["decision"] == "declined":
            raise HTTPException(status_code=409, detail="已回避任务不能再确认")
        now = datetime.now(timezone.utc).isoformat()
        reason = body.reason.strip() if new_state == "declined" else None
        conn.execute(
            "INSERT INTO decisions(paper_id, reviewer_id, decision, reason, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(paper_id, reviewer_id) DO UPDATE SET decision = excluded.decision,"
            " reason = excluded.reason, updated_at = excluded.updated_at",
            (body.paper_id, reviewer_id, new_state, reason, now),
        )
        rev = db.bump_revision(conn)
    return {
        "ok": True,
        "paper_id": body.paper_id,
        "reviewer_id": reviewer_id,
        "decision": new_state,
        "reason": reason,
        "changed": True,
        "revision": rev,
    }


@app.get("/reviewer/decisions")
def list_own_decisions(reviewer_id: str = Depends(require_reviewer)):
    """评审人查看本人在当前发布方案内各分配上的决定状态。"""
    with db.read_txn() as conn:
        pub = _get_published(conn)
        if pub is None:
            return {"reviewer_id": reviewer_id, "decisions": []}
        plan = json.loads(pub["plan"])
        mine_pids = sorted(pid for pid, rs in plan.items() if reviewer_id in rs)
        out = []
        for pid in mine_pids:
            row = conn.execute(
                "SELECT decision, reason, updated_at FROM decisions"
                " WHERE paper_id = ? AND reviewer_id = ?",
                (pid, reviewer_id),
            ).fetchone()
            if row is not None:
                out.append(
                    {
                        "paper_id": pid,
                        "decision": row["decision"],
                        "reason": row["reason"],
                        "updated_at": row["updated_at"],
                    }
                )
    return {"reviewer_id": reviewer_id, "decisions": out}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
