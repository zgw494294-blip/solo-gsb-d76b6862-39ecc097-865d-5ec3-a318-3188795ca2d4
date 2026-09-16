"""SQLite 连接管理与表结构初始化 / 演示数据。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = os.environ.get("CUE_DB_PATH", "/data/cues.db")

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    department  TEXT NOT NULL,
    name        TEXT NOT NULL,
    duration    REAL NOT NULL CHECK(duration >= 0),
    locked_start REAL CHECK(locked_start IS NULL OR locked_start >= 0),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dependencies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cue_id       INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
    depends_on   INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
    delay        REAL NOT NULL DEFAULT 0 CHECK(delay >= 0),
    UNIQUE(cue_id, depends_on),
    CHECK(cue_id <> depends_on)
);

CREATE INDEX IF NOT EXISTS idx_dep_cue ON dependencies(cue_id);
CREATE INDEX IF NOT EXISTS idx_dep_on  ON dependencies(depends_on);

-- ------------------------------------------------------------ 排演执行台
-- 一场排演（runs）。status: running=进行中，ended=已结束；
-- 同一时刻最多一场 running（由应用层在事务内保证）。
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','ended')),
    note         TEXT NOT NULL DEFAULT '',
    origin_epoch REAL NOT NULL,          -- 本场起点（unix 秒，浮点）
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT,
    end_offset   REAL                    -- 结束时相对起点的秒数
);

-- 开场瞬间的提示快照（后续编辑提示单不影响已开始的场次）
CREATE TABLE IF NOT EXISTS run_cues (
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    cue_id       INTEGER NOT NULL,      -- 原提示 id（快照内用它作为标识）
    department   TEXT NOT NULL,
    name         TEXT NOT NULL,
    duration     REAL NOT NULL,
    locked_start REAL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    -- 快照时冻结的计划时间
    plan_start   REAL,
    plan_end     REAL,
    -- 执行记录（相对本场起点的秒数）；status: waiting/running/completed
    status       TEXT NOT NULL DEFAULT 'waiting'
                 CHECK(status IN ('waiting','running','completed')),
    actual_start REAL,
    actual_end   REAL,
    PRIMARY KEY (run_id, cue_id)
);

-- 开场瞬间的依赖与延迟快照
CREATE TABLE IF NOT EXISTS run_deps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    cue_id     INTEGER NOT NULL,      -- 后继（快照 id）
    depends_on INTEGER NOT NULL,      -- 前置（快照 id）
    delay      REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_run_cues_run ON run_cues(run_id);
CREATE INDEX IF NOT EXISTS idx_run_deps_run ON run_deps(run_id);

-- 数据库级保证：同一时刻最多一场进行中的排演（部分唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_single_active
    ON runs(status) WHERE status = 'running';
"""


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """创建表结构（容器启动时执行）。"""
    with db() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


# (部门, 名称, 时长, 锁定开场秒数或 None)
_DEMO_CUES = [
    ("灯光", "开场灯光预设", 20, 0.0),
    ("音响", "序曲播放", 90, None),
    ("机械", "幕布升起", 15, None),
    ("舞监", "演员入场", 30, None),
    ("灯光", "第一幕灯光", 120, None),
    ("音响", "第一幕配乐", 150, None),
    ("舞监", "场景切换A", 25, None),
    ("机械", "转台旋转", 40, 60.0),  # 锁定 60s，早于依赖链允许时间 -> 冲突演示
]

# (cue 序号(1-based), 前置序号, 延迟秒数)
_DEMO_DEPS = [
    (2, 1, 0.0),
    (3, 1, 5.0),
    (4, 2, 0.0),
    (4, 3, 0.0),
    (5, 4, 0.0),
    (6, 2, 10.0),
    (7, 5, 0.0),
    (8, 6, 0.0),
    (8, 7, 0.0),
]


def seed_demo(force: bool = False) -> None:
    """仅在空库（或 force）时写入演示数据。"""
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM cues").fetchone()["c"]
        if count > 0 and not force:
            return
        conn.executescript(_SCHEMA)
        if force:
            conn.execute("DELETE FROM dependencies")
            conn.execute("DELETE FROM cues")
        ids: list[int] = []
        for i, (dept, name, dur, locked) in enumerate(_DEMO_CUES):
            cur = conn.execute(
                "INSERT INTO cues (department, name, duration, locked_start, sort_order)"
                " VALUES (?, ?, ?, ?, ?)",
                (dept, name, dur, locked, i),
            )
            ids.append(cur.lastrowid)
        for cue_idx, pre_idx, delay in _DEMO_DEPS:
            conn.execute(
                "INSERT INTO dependencies (cue_id, depends_on, delay) VALUES (?, ?, ?)",
                (ids[cue_idx - 1], ids[pre_idx - 1], delay),
            )
        conn.commit()
