"""用 stub 导入 app.main，验证 HTTP 路由层（路由表 + 真实 SQLite + 序列化）。"""
import json
import os
import sys
import tempfile
import time
import types

# ---------------------------------------------------------------- pydantic stub

pydantic = types.ModuleType("pydantic")


class FieldInfo:
    def __init__(self, default=None, **kw):
        self.default = default
        self.kw = kw


def Field(default=None, **kw):
    return FieldInfo(default, **kw)


def field_validator(*names, **kw):
    def deco(fn):
        fn._validator_fields = names
        return fn
    return deco


class BaseModel:
    def __init__(self, **data):
        cls = type(self)
        annotations = {}
        for klass in reversed(cls.__mro__):
            annotations.update(getattr(klass, "__annotations__", {}))
        vals = {}
        for name, ann in annotations.items():
            default = getattr(cls, name, None)
            if isinstance(default, FieldInfo):
                default = default.default
            if name in data:
                v = data[name]
            else:
                if default.__class__.__name__ == "list":
                    v = []
                else:
                    v = default
            # 运行 validator（按定义顺序，依赖 CueBase._strip 先于 _unique_preds）
            for attr in vars(cls).values():
                if callable(attr) and getattr(attr, "_validator_fields", None):
                    if name in attr._validator_fields:
                        v = attr(cls, v)
            vals[name] = v
        for name in ("department", "name"):
            if name in vals and isinstance(vals[name], str):
                vals[name] = vals[name].strip()
        self.__dict__.update(vals)
        for name, v in vals.items():
            setattr(self, name, v)


pydantic.BaseModel = BaseModel
pydantic.Field = Field
pydantic.field_validator = field_validator
sys.modules["pydantic"] = pydantic

# ---------------------------------------------------------------- fastapi stubs

fastapi = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


ROUTES = []


class FastAPI:
    def __init__(self, **kw):
        self.startup_handlers = []

    def on_event(self, name):
        def deco(fn):
            if name == "startup":
                self.startup_handlers.append(fn)
            return fn
        return deco

    def _reg(self, method, path, fn, **kw):
        ROUTES.append((method, path, fn))
        return fn

    def get(self, path, **kw):
        return lambda fn: self._reg("GET", path, fn, **kw)

    def post(self, path, **kw):
        return lambda fn: self._reg("POST", path, fn, **kw)

    def put(self, path, **kw):
        return lambda fn: self._reg("PUT", path, fn, **kw)

    def delete(self, path, **kw):
        return lambda fn: self._reg("DELETE", path, fn, **kw)

    def mount(self, *a, **k):
        pass

    def exception_handler(self, *a, **k):
        return lambda fn: fn


class FileResponse:
    def __init__(self, path, media_type=None, filename=None):
        self.path = path
        self.media_type = media_type
        self.filename = filename


class JSONResponse(Exception):
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code
        super().__init__(json.dumps(content, ensure_ascii=False))


fastapi.FastAPI = FastAPI
fastapi.HTTPException = HTTPException


def File(default=None, **kw):
    return default


def Form(default=None, **kw):
    return default


class UploadFile:
    """测试用 UploadFile：filename / content_type + 可分块读取的字节流。"""

    def __init__(self, filename, content, content_type="application/octet-stream"):
        self.filename = filename
        self.content_type = content_type
        self._buf = content
        self._pos = 0

    async def read(self, size=-1):
        if self._pos >= len(self._buf):
            return b""
        if size is None or size < 0:
            chunk = self._buf[self._pos:]
            self._pos = len(self._buf)
        else:
            chunk = self._buf[self._pos:self._pos + size]
            self._pos += len(chunk)
        return chunk


fastapi.File = File
fastapi.Form = Form
fastapi.UploadFile = UploadFile
fastapi.responses = types.ModuleType("fastapi.responses")
fastapi.responses.FileResponse = FileResponse
fastapi.responses.JSONResponse = JSONResponse
fastapi.staticfiles = types.ModuleType("fastapi.staticfiles")
fastapi.staticfiles.StaticFiles = lambda **kw: None
sys.modules["fastapi"] = fastapi
sys.modules["fastapi.responses"] = fastapi.responses
sys.modules["fastapi.staticfiles"] = fastapi.staticfiles

uvicorn = types.ModuleType("uvicorn")
uvicorn.run = lambda *a, **k: None
sys.modules["uvicorn"] = uvicorn

# ---------------------------------------------------------------- 导入应用

tmpdir = tempfile.mkdtemp()
os.environ["CUE_DB_PATH"] = os.path.join(tmpdir, "http.db")
os.environ["CUE_MEDIA_DIR"] = os.path.join(tmpdir, "media")
os.environ["CUE_MEDIA_MAX_BYTES"] = "1048576"  # 测试上限 1MB
os.environ["SEED_DEMO"] = "1"
sys.path.insert(0, os.path.dirname(__file__))

from app import main  # noqa: E402

for h in main.app.startup_handlers:
    h()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def find(method, prefix):
    return [r for r in ROUTES if r[0] == method and r[1].startswith(prefix)]


print("== 路由注册 ==")
expected = [
    ("GET", "/api/health"), ("GET", "/api/schedule"),
    ("POST", "/api/runs"), ("GET", "/api/runs/active"),
    ("GET", "/api/runs/latest"), ("GET", "/console"),
]
for m, p in expected:
    check(f"{m} {p} 已注册", any(x[0] == m and x[1] == p for x in ROUTES))
check("start/complete/end 路由齐全",
      len(find("POST", "/api/runs/")) == 3
      and any("{cue_id}" in r[1] and r[1].endswith("/start") for r in ROUTES)
      and any("{cue_id}" in r[1] and r[1].endswith("/complete") for r in ROUTES)
      and any(r[1].endswith("/end") for r in ROUTES))

print("== 启动初始化（演示数据已写入） ==")
sched = main.get_schedule()
check("schedule 返回 8 条演示提示", len(sched["cues"]) == 8)
health = main.health()
check("health ok", health == {"status": "ok"})

print("== 排演 HTTP 流程 ==")
run = main.start_run(main.RunStart(note="HTTP测试"))
rid = run["id"]
check("POST /api/runs 返回进行中视图", run["status"] == "running"
      and run["counts"]["total"] == 8)
active = main.get_active_run()
check("GET active 命中", active["run"] and active["run"]["id"] == rid)

# cue1 立即就绪 -> start -> complete
r2 = main.cue_start(rid, 1)
check("开始 cue1 -> running",
      next(c for c in r2["cues"] if c["id"] == 1)["status"] == "running")
r3 = main.cue_complete(rid, 1)
check("完成 cue1 -> completed",
      next(c for c in r3["cues"] if c["id"] == 1)["status"] == "completed")

print("== HTTP 层 409 ==")
for name, fn in [
    ("重复开始整场", lambda: main.start_run(main.RunStart(note=""))),
    ("未就绪开始 cue4（前置未完成）", lambda: main.cue_start(rid, 4)),
    ("直接完成未开始 cue4", lambda: main.cue_complete(rid, 4)),
]:
    try:
        fn()
        check(name, False, "未抛 409")
    except HTTPException as e:
        check(f"{name} -> 409", e.status_code == 409)

ended = main.end_run(rid)
check("结束本场", ended["status"] == "ended" and ended["elapsed"] >= 0)
try:
    main.cue_start(rid, 2)
    check("结束后操作 -> 409", False)
except HTTPException as e:
    check("结束后操作 -> 409", e.status_code == 409)

print("== HTTP 层 404 ==")
for name, fn in [
    ("GET 不存在场", lambda: main.get_run(424242)),
    ("GET 不存在提示", lambda: main.get_cue(424242)),
]:
    try:
        fn()
        check(name, False)
    except HTTPException as e:
        check(f"{name} -> 404", e.status_code == 404)

print("== 静态页 ==")
check("/ 返回 index.html",
      str(main.index().path).endswith("index.html"))
check("/console 返回 console.html",
      str(main.console().path).endswith("console.html"))
check("/media 返回 media.html",
      str(main.media_page().path).endswith("media.html"))

print("== 素材库：流式上传 / 去重 / 绑定 / 回收 ==")
import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402
from app import media as M  # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# 1x1 透明 PNG（含完整 PNG 签名与 IEND）
PNG = (b"\x89PNG\r\n\x1a\n"
       + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       + b"\x00\x00\x00\x1f\x15\xc4\x89"
       + b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-"
       + b"\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x44" * 64

m1 = run(main.upload_media(
    file=main.UploadFile("灯光预设.png", PNG, "image/png"),
    name="开场灯光图"))
check("上传 PNG 返回条目", m1["kind"] == "image" and m1["ready"]
      and m1["name"] == "开场灯光图")
import hashlib as _hl  # noqa: E402
_sha = _hl.sha256(PNG).hexdigest()
check("实体按内容寻址落盘且只一份",
      M.entity_path(_sha, ".png").exists()
      and len(list(Path(os.environ["CUE_MEDIA_DIR"]).rglob(f"{_sha}.png"))) == 1)
check("临时目录无残留",
      not list(Path(os.environ["CUE_MEDIA_DIR"], "tmp").glob("*.part")))

# 相同哈希再建一个条目（不同名称）
m1b = run(main.upload_media(
    file=main.UploadFile("另存.png", PNG, "image/png"), name="灯光图副本"))
check("同哈希第二次上传 -> 同实体新条目",
      m1b["sha256"] == m1["sha256"] and m1b["id"] != m1["id"])
with main.database.db() as _c:
    _ent_n = _c.execute("SELECT COUNT(*) c FROM media_entities").fetchone()["c"]
    _it_n = _c.execute("SELECT COUNT(*) c FROM media_items").fetchone()["c"]
check("实体 1 个、条目 2 个", _ent_n == 1 and _it_n == 2)

print("== 素材库：上传校验失败不留痕 ==")
for nm, ct, body in [
    ("坏.exe", "application/octet-stream", b"MZ\x90\x00"),
    ("伪装.png", "image/png", b"MZ\x90\x00xxxx"),
    ("无扩展", "application/octet-stream", b"xxxx"),
    ("空.png", "image/png", b""),
]:
    try:
        run(main.upload_media(main.UploadFile(nm, body, ct), name=None))
        check(f"拒绝 {nm}", False)
    except HTTPException as e:
        check(f"拒绝 {nm} -> {e.status_code}",
              e.status_code in (400, 413, 415))
check("失败后无临时文件、无空记录",
      not list(Path(os.environ["CUE_MEDIA_DIR"], "tmp").glob("*.part")))
with main.database.db() as _c:
    check("失败后实体 / 条目数不变",
          _c.execute("SELECT COUNT(*) c FROM media_entities").fetchone()["c"] == 1
          and _c.execute("SELECT COUNT(*) c FROM media_items").fetchone()["c"] == 2)

# 超限
big = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024)
try:
    run(main.upload_media(main.UploadFile("大.png", big, "image/png"), name=None))
    check("超限文件 -> 413", False)
except HTTPException as e:
    check("超限文件 -> 413", e.status_code == 413)
check("超限后临时文件已清理",
      not list(Path(os.environ["CUE_MEDIA_DIR"], "tmp").glob("*.part")))

print("== 素材库：按名称检索 ==")
run(main.upload_media(main.UploadFile("序曲.mp3", MP3, "audio/mpeg"),
                      name="第一幕序曲"))
lst = main.list_media("序曲")
check("q=序曲 命中 1 条",
      lst["count"] == 1 and lst["items"][0]["name"] == "第一幕序曲")
check("空检索返回全部 3 条", main.list_media(None)["count"] == 3)

print("== 素材库：绑定 / 引用保护 / 末条目回收 ==")
main.bind_cue_media(2, main.CueMediaBinding(media_item_ids=[m1["id"]]))
sched = main.get_schedule()
c2 = next(c for c in sched["cues"] if c["id"] == 2)
check("cue2 已绑定且素材就绪",
      len(c2["media"]) == 1 and c2["media_ready"] is True)

# 被引用条目不可删除
try:
    main.delete_media(m1["id"])
    check("删除被引用条目 -> 409", False)
except HTTPException as e:
    check("删除被引用条目 -> 409", e.status_code == 409)
check("拒绝删除后共享实体仍在", M.entity_path(_sha, ".png").exists())

# 解除绑定 -> 删除第一个条目：同哈希还有 m1b，实体不得回收
main.bind_cue_media(2, main.CueMediaBinding(media_item_ids=[]))
d1 = main.delete_media(m1["id"])
check("删除非末条目：实体保留",
      d1["entity_reclaimed"] is False and M.entity_path(_sha, ".png").exists())

# 删除提示应级联解绑（先绑定到 m1b 再删提示）
main.bind_cue_media(2, main.CueMediaBinding(media_item_ids=[m1b["id"]]))
with main.database.db() as _c:
    _before = _c.execute(
        "SELECT COUNT(*) c FROM cue_media WHERE cue_id=2").fetchone()["c"]
check("删除提示前绑定存在", _before == 1)

# 删除最后一个同哈希条目 -> 回收实体文件
main.bind_cue_media(2, main.CueMediaBinding(media_item_ids=[]))
d2 = main.delete_media(m1b["id"])
check("删除末条目：实体回收",
      d2["entity_reclaimed"] is True and not M.entity_path(_sha, ".png").exists())

print("== 素材库：完整性检查与未就绪传播 ==")
m3 = run(main.upload_media(
    main.UploadFile("序曲2.mp3", MP3, "audio/mpeg"), name="未就绪测试"))
main.bind_cue_media(3, main.CueMediaBinding(media_item_ids=[m3["id"]]))
# 模拟实体文件缺失
M.entity_path(m3["sha256"], ".mp3").unlink()
rep = main.verify_media()
check("校验发现 missing", rep["counts"]["missing"] >= 1
      and not rep["all_ready"])
aff = {a["cue_id"] for a in rep["affected_cues"]}
check("受影响提示包含 cue3", 3 in aff)
c3 = next(c for c in main.get_schedule()["cues"] if c["id"] == 3)
check("cue3 的素材显示未就绪",
      c3["media_ready"] is False and c3["media"][0]["status"] == "missing")
# 排演视图同样反映未就绪
run_obj = main.start_run(main.RunStart(note="素材检查"))
rc3 = next(c for c in run_obj["cues"] if c["id"] == 3)
check("排演视图 cue3 素材未就绪", rc3["media_ready"] is False)
main.end_run(run_obj["id"])

# 重传同内容自愈
m3fix = run(main.upload_media(
    main.UploadFile("序曲2.mp3", MP3, "audio/mpeg"), name="未就绪测试"))
check("重传同哈希：实体修复、复用同一 sha",
      m3fix["sha256"] == m3["sha256"]
      and main.get_media(m3["id"])["ready"] is True)
check("完整性检查恢复 all_ready", main.verify_media()["all_ready"])

print("== 素材库：启动对账清理临时残骸 ==")
stale = Path(os.environ["CUE_MEDIA_DIR"], "tmp", "upload-deadbeef.part")
stale.write_bytes(b"x")
n = M.cleanup_staging()
check("清理到 1 个残留分片", n >= 1 and not stale.exists())

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
