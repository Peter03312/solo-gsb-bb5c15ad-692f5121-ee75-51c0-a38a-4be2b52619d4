"""SQLite 存储层。

- 单连接 + 进程内可重入锁, 写操作使用 BEGIN IMMEDIATE 串行化;
- meta 表维护资料修订号 (revision), 任何论文/评审人资料变更都会使其 +1;
  评审人提交有效决定 (确认/回避) 同样使其 +1;
- published 表保存当前已发布方案及其所依据的修订号; version 为发布版序号,
  每次成功发布 (普通发布或补位发布) 单调 +1, 用于补位发布的版本复核;
- decisions 表保存评审人对已发布分配的确认/回避决定 (按 (论文, 评审人) 持久);
- 发布 = 在同一个写事务内 "校验修订号 -> 重算 -> 写入",
  因此与资料修改并发时不会产生基于旧版资料的方案。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id     TEXT PRIMARY KEY,
    manuscript   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institutions TEXT NOT NULL    -- JSON array
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id  TEXT PRIMARY KEY,
    credential   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institution  TEXT NOT NULL,
    capacity     INTEGER NOT NULL CHECK (capacity >= 1),
    avoid_papers TEXT NOT NULL    -- JSON array
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    revision     INTEGER NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,  -- 发布版序号, 每次成功发布 +1
    plan         TEXT NOT NULL,   -- JSON: {paper_id: [reviewer_id, reviewer_id]}
    explanations TEXT NOT NULL,   -- JSON: 每篇论文的分配与排除原因
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    paper_id    TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('confirmed', 'declined')),
    reason      TEXT,             -- 回避原因 (declined 时非空)
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (paper_id, reviewer_id)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对既有数据库做就地升级 (新增发布版序号列)。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(published)")}
    if "version" not in cols:
        conn.execute("ALTER TABLE published ADD COLUMN version INTEGER NOT NULL DEFAULT 1")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.path.abspath(DB_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0')")
    return _conn


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row["value"])


def bump_revision(conn: sqlite3.Connection) -> int:
    rev = get_revision(conn) + 1
    conn.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(rev),))
    return rev


@contextmanager
def read_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def write_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def reset_for_tests() -> None:
    """关闭并丢弃当前连接 (仅测试使用, 配合重新设置 DB_PATH)。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
