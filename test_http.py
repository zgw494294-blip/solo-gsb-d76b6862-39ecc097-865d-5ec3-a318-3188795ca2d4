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
    def __init__(self, path):
        self.path = path


class JSONResponse(Exception):
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code
        super().__init__(json.dumps(content, ensure_ascii=False))


fastapi.FastAPI = FastAPI
fastapi.HTTPException = HTTPException
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

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
