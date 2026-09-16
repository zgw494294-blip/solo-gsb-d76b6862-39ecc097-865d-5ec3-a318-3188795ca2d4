"""排演执行台核心逻辑。

一场排演开始时冻结当前提示单（提示、依赖、计划时间）的快照，之后对提示单的
任何增删改都不影响该场。同一时刻最多允许一场进行中的排演。

提示执行状态（读取时按场钟实时推导）：

* waiting（等待）：仍有前置未完成，或场钟未到触发时刻；
* ready（就绪）：所有前置均已完成，且会话经过时间已达到
  ``max(各前置实际完成时间 + 对应延迟, 锁定开场时间)``；
  无前置且未锁定的提示开场即就绪；
* running（执行中）：已记录开始、尚未记录完成；
* completed（已完成）：已记录完成。

开始 / 完成时间均以「相对本场起点的秒数」记录在案，非法状态跳转返回 409。
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import HTTPException

from . import database
from . import media as media_mod
from .scheduler import build_schedule

# 状态
WAITING = "waiting"
READY = "ready"
RUNNING = "running"
COMPLETED = "completed"


# ---------------------------------------------------------------- 内部工具

def _latest_run_row(conn):
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def _load_snapshot(conn, run_id: int) -> tuple[list[dict], list[dict]]:
    cues = [dict(r) for r in conn.execute(
        "SELECT cue_id AS id, department, name, duration, locked_start, "
        "sort_order, plan_start, plan_end, status, actual_start, actual_end "
        "FROM run_cues WHERE run_id = ?", (run_id,)).fetchall()]
    deps = [dict(r) for r in conn.execute(
        "SELECT cue_id, depends_on, delay FROM run_deps WHERE run_id = ?",
        (run_id,)).fetchall()]
    return cues, deps


def _now_offset(origin_epoch: float) -> float:
    return round(max(0.0, time.time() - origin_epoch), 3)


def _derive_states(
    snap_cues: list[dict],
    snap_deps: list[dict],
    elapsed: float,
) -> dict[int, dict]:
    """按快照依赖与实际执行记录推导每条提示的实时状态。

    返回 cue_id -> {status, ready_in, threshold, preds: [{id, delay, done}]}。
    """
    ids = {c["id"] for c in snap_cues}
    preds: dict[int, list[tuple[int, float]]] = {cid: [] for cid in ids}
    for d in snap_deps:
        if d["cue_id"] in ids and d["depends_on"] in ids:
            preds[d["cue_id"]].append((d["depends_on"], float(d["delay"])))

    stored = {c["id"]: c for c in snap_cues}
    info: dict[int, dict] = {}

    # 状态判定只依赖前置「已完成」记录的 actual_end，无需严格拓扑序；
    # 快照在开场时已做过成环防御，这里按 id 稳定遍历即可。
    for cid in sorted(ids):
        c = stored[cid]
        plist = preds.get(cid, [])
        pred_info = []

        # 触发时刻 = max(各前置实际完成时间 + 延迟, 锁定开场时间)；
        # 无前置未锁定时触发时刻为 0（开场即就绪）。
        dep_threshold = None
        preds_all_done = True
        if plist:
            dep_threshold = 0.0
            for pid, delay in plist:
                pre = stored.get(pid)
                done = pre is not None and pre["status"] == COMPLETED
                pred_info.append({"id": pid, "delay": round(delay, 3),
                                  "done": done})
                if not done:
                    preds_all_done = False
                elif pre["actual_end"] is not None:
                    dep_threshold = max(
                        dep_threshold, float(pre["actual_end"]) + delay)

        candidates = []
        if dep_threshold is not None:
            candidates.append(dep_threshold)
        if c["locked_start"] is not None:
            candidates.append(float(c["locked_start"]))
        threshold = max(candidates) if candidates else 0.0

        if c["status"] == COMPLETED:
            status = COMPLETED
        elif c["status"] == RUNNING:
            status = RUNNING
        else:  # 库中 waiting；是否已就绪看前置与场钟
            no_gate = not plist and c["locked_start"] is None
            if no_gate or (preds_all_done and elapsed + 1e-6 >= threshold):
                status = READY
            else:
                status = WAITING

        ready_in = None
        if status == WAITING:
            if preds_all_done:  # 前置均已完成，只差场钟到点
                ready_in = round(max(0.0, threshold - elapsed), 1)
            # 尚有前置未完成 -> ready_in 保持 null（无法预估）
        info[cid] = {
            "status": status,
            "ready_in": ready_in,
            "threshold": round(threshold, 3),
            "preds": pred_info,
        }
    return info


def _assemble(conn, row) -> dict:
    """组装一场排演的完整视图（含实时状态 / 偏差）。"""
    run_id = row["id"]
    snap_cues, snap_deps = _load_snapshot(conn, run_id)
    origin = float(row["origin_epoch"])

    if row["status"] == "running":
        elapsed = _now_offset(origin)
        wall_open = time.strftime("%H:%M:%S",
                                  time.localtime(origin))
    else:
        elapsed = float(row["end_offset"] or 0.0)
        wall_open = time.strftime("%H:%M:%S",
                                  time.localtime(origin))

    # 用快照数据复算计划排程（start/end/冲突链），保证与提示单同口径
    raw_cues = [{
        "id": c["id"], "department": c["department"], "name": c["name"],
        "duration": c["duration"], "locked_start": c["locked_start"],
        "sort_order": c["sort_order"],
    } for c in snap_cues]
    raw_edges = [{"cue_id": d["cue_id"], "depends_on": d["depends_on"],
                  "delay": d["delay"]} for d in snap_deps]
    sched = build_schedule(raw_cues, raw_edges)

    derived = _derive_states(snap_cues, snap_deps, elapsed)
    stored_map = {c["id"]: c for c in snap_cues}

    # 素材就绪状态为**实时**读取（不进快照）：实体缺失 / 哈希不符时，
    # 即使是开场前已绑定素材的历史场次，引用提示也立刻显示「素材未就绪」。
    media_map = media_mod.attach_media_to_cues(
        conn, [pc["id"] for pc in sched["cues"]])

    cues_out = []
    for pc in sched["cues"]:
        cid = pc["id"]
        st = stored_map[cid]
        dv = derived[cid]
        # 计划时间严格使用开场冻结值（sched 仅用于冲突链/边/排序口径一致）
        pc = {**pc, "start": st["plan_start"], "end": st["plan_end"]}
        actual_start = st["actual_start"]
        actual_end = st["actual_end"]
        start_dev = (round(actual_start - pc["start"], 3)
                     if actual_start is not None and pc["start"] is not None
                     else None)
        if actual_end is not None and pc["end"] is not None:
            end_dev = round(actual_end - pc["end"], 3)
        elif dv["status"] == RUNNING and pc["end"] is not None:
            # 执行中：按当前场钟估算「若此刻完成」的结束偏差
            end_dev = round(elapsed - pc["end"], 3)
        else:
            end_dev = None
        cues_out.append({
            **pc,
            "status": dv["status"],
            "ready_in": dv["ready_in"],
            "ready_threshold": dv["threshold"],
            "actual_start": actual_start,
            "actual_end": actual_end,
            "start_deviation": start_dev,
            "end_deviation": end_dev,
            "media": media_map.get(cid, []),
            "media_ready": all(m["ready"] for m in media_map.get(cid, [])),
        })

    done = sum(1 for c in snap_cues if c["status"] == COMPLETED)
    return {
        "id": run_id,
        "status": row["status"],
        "note": row["note"],
        "origin_epoch": round(origin, 3),
        "server_epoch": round(time.time(), 3),
        "elapsed": round(elapsed, 3),
        "wall_clock_open": wall_open,
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "cues": cues_out,
        "edges": sched["edges"],
        "conflicts": sched["conflicts"],
        "counts": {
            "total": len(snap_cues),
            "waiting": sum(1 for c in cues_out if c["status"] == WAITING),
            "ready": sum(1 for c in cues_out if c["status"] == READY),
            "running": sum(1 for c in cues_out if c["status"] == RUNNING),
            "completed": done,
        },
    }


# ---------------------------------------------------------------- API 操作

def start_run(note: str = "") -> dict:
    """开始一场排演：冻结快照，记录起点。已有进行中的场次则 409。"""
    with database.db() as conn:
        try:
            active = conn.execute(
                "SELECT id FROM runs WHERE status = 'running'").fetchone()
            if active is not None:
                raise HTTPException(
                    409, f"已有进行中的排演（第 {active['id']} 场），"
                         "请先结束本场后再开始新的排演")

            cues = [dict(r) for r in conn.execute(
                "SELECT id, department, name, duration, locked_start, "
                "sort_order FROM cues ORDER BY sort_order, id").fetchall()]
            if not cues:
                raise HTTPException(409, "提示单为空，无法开始排演")
            edges = [dict(r) for r in conn.execute(
                "SELECT cue_id, depends_on, delay FROM dependencies")
                .fetchall()]

            # 快照内复算计划时间（同时承担成环防御）
            sched = build_schedule(cues, edges)
            if sched["cycle"]:
                raise HTTPException(
                    409, "依赖关系中存在环，无法开始排演: "
                         + ", ".join(map(str, sched["cycle"])))

            origin = time.time()
            try:
                cur = conn.execute(
                    "INSERT INTO runs (status, note, origin_epoch) "
                    "VALUES ('running', ?, ?)", (note, origin))
            except sqlite3.IntegrityError:
                # 部分唯一索引：并发下已存在进行中的场次
                raise HTTPException(
                    409, "已有进行中的排演，请先结束本场后再开始新的排演")
            run_id = cur.lastrowid

            plan_map = {c["id"]: c for c in sched["cues"]}
            for c in cues:
                p = plan_map[c["id"]]
                conn.execute(
                    "INSERT INTO run_cues (run_id, cue_id, department, name, "
                    "duration, locked_start, sort_order, plan_start, plan_end)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, c["id"], c["department"], c["name"],
                     c["duration"], c["locked_start"], c["sort_order"],
                     p["start"], p["end"]))
            for e in edges:
                conn.execute(
                    "INSERT INTO run_deps (run_id, cue_id, depends_on, delay)"
                    " VALUES (?, ?, ?, ?)",
                    (run_id, e["cue_id"], e["depends_on"], e["delay"]))
            conn.commit()
            row = conn.execute("SELECT * FROM runs WHERE id = ?",
                               (run_id,)).fetchone()
            return _assemble(conn, row)
        except Exception:
            conn.rollback()
            raise


def get_active_run() -> dict | None:
    """当前进行中的排演；没有时返回 None。"""
    with database.db() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'running' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return _assemble(conn, row)


def get_latest_run() -> dict | None:
    """最近一场排演（含已结束），供刷新后继续展示同一场。"""
    with database.db() as conn:
        row = _latest_run_row(conn)
        if row is None:
            return None
        return _assemble(conn, row)


def get_run(run_id: int) -> dict:
    with database.db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "排演不存在")
        return _assemble(conn, row)


def _require_active_cue(conn, run_id: int, cue_id: int):
    row = conn.execute("SELECT * FROM runs WHERE id = ?",
                       (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "排演不存在")
    if row["status"] != "running":
        raise HTTPException(409, "本场排演已结束，不能再记录执行操作")
    cue = conn.execute(
        "SELECT * FROM run_cues WHERE run_id = ? AND cue_id = ?",
        (run_id, cue_id)).fetchone()
    if cue is None:
        raise HTTPException(404, "该提示不在本场快照中")
    return row, cue


def cue_start(run_id: int, cue_id: int) -> dict:
    """记录提示开始（仅就绪 -> 执行中合法）。"""
    with database.db() as conn:
        try:
            row, cue = _require_active_cue(conn, run_id, cue_id)
            if cue["status"] == COMPLETED:
                raise HTTPException(409, f"提示 #{cue_id} 已完成，不能重复开始")
            if cue["status"] == RUNNING:
                raise HTTPException(409, f"提示 #{cue_id} 已在执行中")
            snap_cues, snap_deps = _load_snapshot(conn, run_id)
            elapsed = _now_offset(float(row["origin_epoch"]))
            derived = _derive_states(snap_cues, snap_deps, elapsed)
            if derived[cue_id]["status"] != READY:
                reason = "仍有前置提示未完成"
                if derived[cue_id]["ready_in"] is not None:
                    reason = (f"未到触发时刻（还需约 "
                              f"{derived[cue_id]['ready_in']:.0f} 秒）")
                raise HTTPException(
                    409, f"提示 #{cue_id} 尚未就绪：{reason}")
            # 条件更新：并发情况下仅当仍为 waiting 时生效，防止重复开始
            cur = conn.execute(
                "UPDATE run_cues SET status = 'running', actual_start = ? "
                "WHERE run_id = ? AND cue_id = ? AND status = 'waiting'",
                (elapsed, run_id, cue_id))
            if cur.rowcount == 0:
                raise HTTPException(409, f"提示 #{cue_id} 状态已变化，请刷新后重试")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return get_run(run_id)


def cue_complete(run_id: int, cue_id: int) -> dict:
    """记录提示完成（仅执行中 -> 已完成合法）。"""
    with database.db() as conn:
        try:
            row, cue = _require_active_cue(conn, run_id, cue_id)
            if cue["status"] == WAITING:
                raise HTTPException(
                    409, f"提示 #{cue_id} 尚未开始，不能直接完成")
            if cue["status"] == COMPLETED:
                raise HTTPException(409, f"提示 #{cue_id} 已完成，请勿重复操作")
            elapsed = _now_offset(float(row["origin_epoch"]))
            # 条件更新：并发情况下仅当仍为 running 时生效，防止重复完成
            cur = conn.execute(
                "UPDATE run_cues SET status = 'completed', actual_end = ? "
                "WHERE run_id = ? AND cue_id = ? AND status = 'running'",
                (elapsed, run_id, cue_id))
            if cur.rowcount == 0:
                raise HTTPException(409, f"提示 #{cue_id} 状态已变化，请刷新后重试")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return get_run(run_id)


def end_run(run_id: int) -> dict:
    """结束本场（进行中 -> 已结束），记录相对起点的结束秒数。"""
    with database.db() as conn:
        try:
            row = conn.execute("SELECT * FROM runs WHERE id = ?",
                               (run_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "排演不存在")
            if row["status"] != "running":
                raise HTTPException(409, "本场排演已经结束")
            offset = _now_offset(float(row["origin_epoch"]))
            cur = conn.execute(
                "UPDATE runs SET status = 'ended', ended_at = datetime('now'),"
                " end_offset = ? WHERE id = ? AND status = 'running'",
                (offset, run_id))
            if cur.rowcount == 0:
                raise HTTPException(409, "本场排演已被结束")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return get_run(run_id)
