"""不依赖 FastAPI 的排演核心端到端测试（用临时 SQLite 库）。"""
import os
import sys
import tempfile
import time
import types

# --- stub fastapi.HTTPException（rehearsal.py 仅用到它） ---
fastapi = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


fastapi.HTTPException = HTTPException
sys.modules["fastapi"] = fastapi

tmpdir = tempfile.mkdtemp()
os.environ["CUE_DB_PATH"] = os.path.join(tmpdir, "test.db")
os.environ["SEED_DEMO"] = "0"

sys.path.insert(0, os.path.dirname(__file__))
from app import database, rehearsal  # noqa: E402

database.init_db()
database.seed_demo(force=True)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def expect_409(name, fn):
    try:
        fn()
        check(name, False, "（未返回 409）")
    except HTTPException as e:
        check(name + f" [409: {e.detail[:40]}]", e.status_code == 409)
    except Exception as e:  # noqa
        check(name, False, f"（异常 {type(e).__name__}: {e}）")


print("== 1. 初始无场次 ==")
check("无进行中场次", rehearsal.get_active_run() is None)
check("无最近场次", rehearsal.get_latest_run() is None)

print("== 2. 开始一场，快照计划 ==")
run = rehearsal.start_run("联排A")
rid = run["id"]
check("进行中", run["status"] == "running")
check("8 条快照", len(run["cues"]) == 8)
by_id = {c["id"]: c for c in run["cues"]}
# cue1 锁定 0、无前置 -> 计划 0
check("cue1 计划开始=0", by_id[1]["start"] == 0)
check("cue1 立即就绪", by_id[1]["status"] == "ready")
# cue2 依赖 cue1（delay 0），cue1 未完成 -> waiting
check("cue2 等待", by_id[2]["status"] == "waiting")
check("cue8 等待", by_id[8]["status"] == "waiting")
# 演示数据中 cue8 锁定60 与依赖冲突应被冻结保留
conf_ids = {c["cue_id"] for c in run["conflicts"]}
check("快照保留锁定冲突 #8", 8 in conf_ids)

print("== 3. 非法跳转 ==")
expect_409("未就绪不能开始(cue2)", lambda: rehearsal.cue_start(rid, 2))
expect_409("未开始不能完成(cue1)", lambda: rehearsal.cue_complete(rid, 1))
expect_409("重复开始进行中场", lambda: rehearsal.start_run())

print("== 4. 推进 cue1，验证就绪级联 ==")
run = rehearsal.cue_start(rid, 1)
check("cue1 执行中", next(c for c in run["cues"] if c["id"] == 1)["status"] == "running")
expect_409("重复开始 cue1", lambda: rehearsal.cue_start(rid, 1))
expect_409("cue2 仍不能开始（cue1 未完成）", lambda: rehearsal.cue_start(rid, 2))

# cue3 依赖 cue1 + delay 5；cue2 依赖 cue1 + delay 0
time.sleep(0.05)
run = rehearsal.cue_complete(rid, 1)
c1 = next(c for c in run["cues"] if c["id"] == 1)
check("cue1 已完成且记录 actual_start/end",
      c1["status"] == "completed" and c1["actual_start"] is not None
      and c1["actual_end"] is not None)
# cue1 计划结束=20s（开场即触发），实际约 0s 完成 -> 提前约 20s，属正常
check("cue1 开始偏差≈0", abs(c1["start_deviation"]) < 0.5, c1["start_deviation"])
check("cue1 结束偏差≈-20（提前完成）", abs(c1["end_deviation"] + 20) < 0.6,
      c1["end_deviation"])
check("cue2 已就绪（前置完成）",
      next(c for c in run["cues"] if c["id"] == 2)["status"] == "ready")
# cue3 需要 cue1 完成后再等 5 秒
c3 = next(c for c in run["cues"] if c["id"] == 3)
check("cue3 仍等待（5s 延迟未到）", c3["status"] == "waiting")
check("cue3 ready_in≈5", c3["ready_in"] is not None and 4.5 <= c3["ready_in"] <= 5.1,
      c3["ready_in"])

print("== 5. 等待 5s 延迟窗口 ==")
time.sleep(5.2)
run = rehearsal.get_run(rid)
c3 = next(c for c in run["cues"] if c["id"] == 3)
check("cue3 延迟后就绪", c3["status"] == "ready", c3["status"])

# cue4 依赖 cue2 与 cue3，二者未完 -> 等待
check("cue4 等待（cue2/cue3 未完）",
      next(c for c in run["cues"] if c["id"] == 4)["status"] == "waiting")
rehearsal.cue_start(rid, 2)
rehearsal.cue_complete(rid, 2)
rehearsal.cue_start(rid, 3)
rehearsal.cue_complete(rid, 3)
run = rehearsal.get_run(rid)
check("cue4 就绪（双前置均完成）",
      next(c for c in run["cues"] if c["id"] == 4)["status"] == "ready")

print("== 6. 快照隔离：改提示单不影响本场 ==")
import sqlite3  # noqa
with database.db() as conn:
    conn.execute("UPDATE cues SET name='被改名的开场', duration=999 WHERE id=1")
    conn.execute("DELETE FROM dependencies WHERE cue_id=2 AND depends_on=1")
    conn.execute("DELETE FROM cues WHERE id=5")
    conn.commit()
run = rehearsal.get_run(rid)
c1s = next(c for c in run["cues"] if c["id"] == 1)
check("快照名称未变", c1s["name"] == "开场灯光预设", c1s["name"])
check("快照时长未变", c1s["duration"] == 20)
check("快照仍含 cue5", any(c["id"] == 5 for c in run["cues"]))
check("快照依赖边未变(2<-1)",
      any(e["from"] == 1 and e["to"] == 2 for e in run["edges"]))

print("== 7. 结束本场 ==")
run = rehearsal.end_run(rid)
check("已结束", run["status"] == "ended")
check("end_offset 已记录", run["elapsed"] >= 5)
expect_409("结束后不能开始提示", lambda: rehearsal.cue_start(rid, 4))
expect_409("结束后不能完成提示", lambda: rehearsal.cue_complete(rid, 4))
expect_409("重复结束", lambda: rehearsal.end_run(rid))
# 已完成记录仍在
check("已完成数保持 3", run["counts"]["completed"] == 3, run["counts"])

print("== 8. 刷新后恢复 & 开新场 ==")
latest = rehearsal.get_latest_run()
check("latest 仍是上一场（已结束）", latest["id"] == rid and latest["status"] == "ended")
check("active 为空", rehearsal.get_active_run() is None)

run2 = rehearsal.start_run("联排B")
check("新场可开始", run2["id"] != rid and run2["status"] == "running")
check("新场快照反映新提示单（7 条）", len(run2["cues"]) == 7, len(run2["cues"]))
c1b = next((c for c in run2["cues"] if c["id"] == 1), None)
check("新场 cue1 名称为改名后", c1b and c1b["name"] == "被改名的开场")
# cue2 在新提示单里已无前置 -> 立即就绪
c2b = next(c for c in run2["cues"] if c["id"] == 2)
check("新场 cue2 无前置立即就绪", c2b["status"] == "ready")
rehearsal.end_run(run2["id"])

print("== 9. 不存在资源 ==")
try:
    rehearsal.get_run(9999)
    check("不存在的场 404", False)
except HTTPException as e:
    check("不存在的场 404", e.status_code == 404)
run3 = rehearsal.start_run("联排C")
try:
    rehearsal.cue_start(run3["id"], 9999)
    check("进行中场中不存在的提示 404", False)
except HTTPException as e:
    check("进行中场中不存在的提示 404", e.status_code == 404)
rehearsal.end_run(run3["id"])
# 已结束场次中操作（无论提示是否存在）-> 409（场次状态优先校验）
expect_409("已结束场次操作 -> 409", lambda: rehearsal.cue_start(rid, 4))
expect_409("已结束场次操作不存在提示 -> 409",
           lambda: rehearsal.cue_start(run2["id"], 9999))

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
