"""提示素材库端到端测试（stub FastAPI，真实 SQLite + 真实文件系统）。

覆盖：流式落盘 / SHA-256 去重 / 扩展名·MIME·魔数·大小校验 / 原子移入 /
引用保护 / 末条目回收实体 / 完整性检查与未就绪传播 / 重传自愈 /
数据库回滚不留空记录 / 并发重复上传不留临时文件。
"""
import asyncio
import hashlib
import os
import sys
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --- stub fastapi（media.py / main.py 仅依赖 HTTPException） ---
fastapi = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


fastapi.HTTPException = HTTPException


class _UploadFile:
    def __init__(self, filename, content, content_type="application/octet-stream",
                 chunk=4096):
        self.filename = filename
        self.content_type = content_type
        self._b = content
        self._p = 0
        self._chunk = chunk

    async def read(self, size=-1):
        if self._p >= len(self._b):
            return b""
        if size is None or size < 0:
            out = self._b[self._p:]
            self._p = len(self._b)
        else:
            out = self._b[self._p:self._p + size]
            self._p += len(out)
        return out


fastapi.UploadFile = _UploadFile
fastapi.File = lambda *a, **k: None
fastapi.Form = lambda *a, **k: None

ROUTES = []


class _FastAPI:
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

    def get(self, p, **kw): return lambda fn: self._reg("GET", p, fn, **kw)
    def post(self, p, **kw): return lambda fn: self._reg("POST", p, fn, **kw)
    def put(self, p, **kw): return lambda fn: self._reg("PUT", p, fn, **kw)
    def delete(self, p, **kw): return lambda fn: self._reg("DELETE", p, fn, **kw)
    def mount(self, *a, **k): pass
    def exception_handler(self, *a, **k): return lambda fn: fn


class _FileResponse:
    def __init__(self, path, media_type=None, filename=None):
        self.path = path
        self.media_type = media_type


class _JSONResponse(Exception):
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code
        super().__init__("json")


fastapi.FastAPI = _FastAPI
fastapi.responses = types.ModuleType("fastapi.responses")
fastapi.responses.FileResponse = _FileResponse
fastapi.responses.JSONResponse = _JSONResponse
fastapi.staticfiles = types.ModuleType("fastapi.staticfiles")
fastapi.staticfiles.StaticFiles = lambda **k: None
sys.modules["fastapi"] = fastapi
sys.modules["fastapi.responses"] = fastapi.responses
sys.modules["fastapi.staticfiles"] = fastapi.staticfiles
uvicorn = types.ModuleType("uvicorn")
uvicorn.run = lambda *a, **k: None
sys.modules["uvicorn"] = uvicorn

tmp = tempfile.mkdtemp()
os.environ["CUE_DB_PATH"] = os.path.join(tmp, "media.db")
os.environ["CUE_MEDIA_DIR"] = os.path.join(tmp, "media")
os.environ["CUE_MEDIA_MAX_BYTES"] = "1048576"  # 1MB
os.environ["SEED_DEMO"] = "0"
sys.path.insert(0, os.path.dirname(__file__))

# --- pydantic stub（与 test_http 同款最小实现） ---
pydantic = types.ModuleType("pydantic")


class _FieldInfo:
    def __init__(self, default=None, **kw):
        self.default = default
        self.kw = kw


def _Field(default=None, **kw):
    return _FieldInfo(default, **kw)


def _field_validator(*names, **kw):
    def deco(fn):
        fn._validator_fields = names
        return fn
    return deco


class _BaseModel:
    def __init__(self, **data):
        cls = type(self)
        annotations = {}
        for klass in reversed(cls.__mro__):
            annotations.update(getattr(klass, "__annotations__", {}))
        vals = {}
        for name, ann in annotations.items():
            default = getattr(cls, name, None)
            if isinstance(default, _FieldInfo):
                default = default.default
            v = data[name] if name in data else ([] if isinstance(default, list) else default)
            for attr in vars(cls).values():
                if callable(attr) and getattr(attr, "_validator_fields", None):
                    if name in attr._validator_fields:
                        v = attr(cls, v)
            vals[name] = v
        self.__dict__.update(vals)
        for name, v in vals.items():
            setattr(self, name, v)


pydantic.BaseModel = _BaseModel
pydantic.Field = _Field
pydantic.field_validator = _field_validator
sys.modules["pydantic"] = pydantic

from app import database, media as M  # noqa: E402
from app import main  # noqa: E402

database.init_db()
M.init_storage()
M.reconcile_on_startup()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def expect_error(name, fn, codes):
    try:
        fn()
        check(name, False, "（未拒绝）")
    except HTTPException as e:
        check(f"{name} -> {e.status_code}", e.status_code in codes,
              f"（得到 {e.status_code}）")
    except Exception as e:  # noqa
        check(name, False, f"（{type(e).__name__}: {e}）")


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


PNG = (b"\x89PNG\r\n\x1a\n"
       + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       + b"\x00\x00\x00\x1f\x15\xc4\x89"
       + b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-"
       + b"\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
JPG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"0" * 64
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x44" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128

root = Path(os.environ["CUE_MEDIA_DIR"])


def tmp_parts():
    return list((root / "tmp").glob("*.part"))


def counts():
    with database.db() as c:
        return (c.execute("SELECT COUNT(*) c FROM media_entities").fetchone()["c"],
                c.execute("SELECT COUNT(*) c FROM media_items").fetchone()["c"])


print("== 1. 流式上传 / 校验 ==")
it = run(main.upload_media(_UploadFile("灯光.png", PNG, "image/png"), name="主光位图"))
sha = hashlib.sha256(PNG).hexdigest()
check("返回就绪图片条目", it["ready"] and it["kind"] == "image"
      and it["sha256"] == sha)
check("实体文件原子落位", M.entity_path(sha, ".png").is_file())
check("临时目录干净", not tmp_parts())
check("声明 octet-stream 但魔数正确可通过",
      (octet_item := run(main.upload_media(_UploadFile("a.mp3", MP3,
          "application/octet-stream"), name=None)))["ready"])

print("== 2. 校验失败不留痕 ==")
e0, i0 = counts()
expect_error("exe 扩展名 415",
             lambda: run(main.upload_media(_UploadFile("x.exe", b"MZ", "application/x-msdownload"))),
             (400, 415))
expect_error("jpg 头配 png 后缀（魔数不符）415",
             lambda: run(main.upload_media(_UploadFile("x.png", JPG_MAGIC, "image/png"))),
             (415,))
expect_error("MIME 与扩展名不符 415",
             lambda: run(main.upload_media(_UploadFile("x.mp4", MP4, "image/png"))),
             (415,))
expect_error("无扩展名 400",
             lambda: run(main.upload_media(_UploadFile("readme", b"x", "text/plain"))),
             (400,))
expect_error("空文件 400",
             lambda: run(main.upload_media(_UploadFile("x.mp3", b"", "audio/mpeg"))),
             (400,))
expect_error("非法名称 400",
             lambda: run(main.upload_media(_UploadFile("x.mp3", MP3, "audio/mpeg"),
                                           name="../evil")),
             (400,))
big = b"ID3" + b"0" * (2 * 1024 * 1024)
expect_error("超过 1MB 上限 413",
             lambda: run(main.upload_media(_UploadFile("big.mp3", big, "audio/mpeg"))),
             (413,))
check("失败后无临时文件", not tmp_parts())
check("失败后无空记录", counts() == (e0, i0))

print("== 3. 去重：同哈希多条目、单实体 ==")
it2 = run(main.upload_media(_UploadFile("副本.png", PNG, "image/png"), name="另一张同名图"))
check("第二次同内容 -> 同 SHA 新条目", it2["sha256"] == sha and it2["id"] != it["id"])
check("物理文件仍只有一份",
      len(list(root.rglob(f"{sha}.png"))) == 1)
ent_n, it_n = counts()
# octet-stream 上传的 MP3 与 MP3 常量同哈希 -> 共享实体
check(f"实体 2 / 条目 3（实际 {ent_n}/{it_n}）", ent_n == 2 and it_n == 3)
print("== 4. 绑定与引用保护 ==")
with database.db() as c:
    c.execute("INSERT INTO cues (department,name,duration) VALUES ('灯光','提示A',10)")
    c.commit()
    cue_a = c.execute("SELECT MAX(id) m FROM cues").fetchone()["m"]
    c.execute("INSERT INTO cues (department,name,duration) VALUES ('音响','提示B',10)")
    c.commit()
    cue_b = c.execute("SELECT MAX(id) m FROM cues").fetchone()["m"]
r = M.set_cue_bindings(cue_a, [it["id"]])
check("cue_a 绑定 1 个素材", len(r["media"]) == 1 and r["media"][0]["ready"])
expect_error("删除被引用条目 409", lambda: M.delete_item(it["id"]), (409,))
check("409 后共享实体文件仍在", M.entity_path(sha, ".png").exists())
expect_error("重复绑定同一素材 400",
             lambda: M.set_cue_bindings(cue_b, [it["id"], it["id"]]), (400,))
expect_error("绑定不存在条目 400",
             lambda: M.set_cue_bindings(cue_b, [99999]), (400,))
# 回滚不留空绑定
with database.db() as c:
    n = c.execute("SELECT COUNT(*) c FROM cue_media WHERE cue_id=?",
                  (cue_b,)).fetchone()["c"]
check("失败绑定回滚无残留", n == 0)

print("== 5. 末条目才回收实体 ==")
M.set_cue_bindings(cue_a, [])
d = M.delete_item(it["id"])
check("删除条目（仍有同哈希条目）实体保留",
      d["entity_reclaimed"] is False and M.entity_path(sha, ".png").exists())
d = M.delete_item(it2["id"])
check("删除最后一个同哈希条目 -> 实体回收",
      d["entity_reclaimed"] is True and not M.entity_path(sha, ".png").exists())
ent_n2, it_n2 = counts()
check("实体行已删除", ent_n2 == 1)  # 仅剩 octet 上传的 mp3 实体

print("== 6. 删除提示级联解绑，实体不回收 ==")
it3 = run(main.upload_media(_UploadFile("v.mp4", MP4, "video/mp4"), name="开场视频"))
M.set_cue_bindings(cue_b, [it3["id"]])
with database.db() as c:
    c.execute("DELETE FROM cues WHERE id=?", (cue_b,))
    c.commit()
_cc = database.get_connection()
check("提示删除后绑定级联清除",
      _cc.execute("SELECT COUNT(*) c FROM cue_media WHERE cue_id=?",
                  (cue_b,)).fetchone()["c"] == 0)
_cc.close()
detail = M.get_item(it3["id"])
check("提示删除不影响素材条目与实体",
      detail["ready"] and M.entity_path(detail["sha256"], ".mp4").exists())

print("== 7. 完整性检查 / 未就绪传播 / 重传自愈 ==")
with database.db() as c:
    c.execute("INSERT INTO cues (department,name,duration) VALUES ('视频','提示C',10)")
    c.commit()
    cue_c = c.execute("SELECT MAX(id) m FROM cues").fetchone()["m"]
M.set_cue_bindings(cue_c, [it3["id"]])
p = M.entity_path(it3["sha256"], ".mp4")
p.unlink()
rep = main.verify_media(deep=True)
check("deep 检查发现 missing",
      rep["counts"]["missing"] >= 1 and not rep["all_ready"])
check("受影响提示列出 cue_c", any(a["cue_id"] == cue_c for a in rep["affected_cues"]))
with database.db() as c:
    ready_map = M.attach_media_to_cues(c, [cue_c])
check("引用提示素材显示未就绪",
      ready_map[cue_c][0]["ready"] is False
      and ready_map[cue_c][0]["status"] == "missing")
it3b = run(main.upload_media(_UploadFile("v.mp4", MP4, "video/mp4"), name="开场视频"))
check("重传同内容自愈：同 SHA、原条目恢复就绪",
      it3b["sha256"] == it3["sha256"] and M.get_item(it3["id"])["ready"])

# 哈希不符：写入截断内容
p2 = M.entity_path(it3["sha256"], ".mp4")
p2.write_bytes(p2.read_bytes()[:-10])
rep2 = main.verify_media()
check("截断文件检出 corrupt",
      any(e["sha256"] == it3["sha256"] and e["status"] == "corrupt"
          for e in rep2["entities"]))
# 恢复
run(main.upload_media(_UploadFile("v.mp4", MP4, "video/mp4"), name="开场视频"))
check("再次重传恢复 all_ready", main.verify_media()["all_ready"])

print("== 8. 启动对账：临时残骸清理 + 状态修正 ==")
(root / "tmp" / "upload-stale.part").write_bytes(b"x")
M.reconcile_on_startup()
check("重启后 .part 被清理", not (root / "tmp" / "upload-stale.part").exists())
check("重启后实体状态重新核对为 ok",
      all(e["status"] == "ok" for e in main.verify_media(deep=False)["entities"]))

print("== 9. 检索 ==")
lst = M.list_items("开场")
check("按名称命中", all("开场" in x["name"] or "开场" in x["original_name"]
                        for x in lst["items"]) and lst["count"] >= 1)

print("== 10. 并发重复上传 ==")
it4 = run(main.upload_media(_UploadFile("song.mp3", MP3, "audio/mpeg"), name="并发曲"))
sha4 = it4["sha256"]


def worker(i):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(main.upload_media(
            _UploadFile(f"s{i}.mp3", MP3, "audio/mpeg"), name=f"并发{i}"))
    finally:
        loop.close()


with ThreadPoolExecutor(8) as ex:
    outs = list(ex.map(worker, range(8)))
with database.db() as c:
    ent = c.execute("SELECT COUNT(*) c FROM media_entities WHERE sha256=?",
                    (sha4,)).fetchone()["c"]
    items_n = c.execute("SELECT COUNT(*) c FROM media_items WHERE entity_sha=?",
                        (sha4,)).fetchone()["c"]
# 第 1 节 octet-stream 上传的 MP3 与本次同哈希：1 + 1 + 8 = 10 条目
check("8 并发同内容：1 实体 10 条目",
      ent == 1 and items_n == 10, f"（{ent}/{items_n}）")
check("物理文件唯一、无临时残留",
      len(list(root.rglob(f"{sha4}.mp3"))) == 1 and not tmp_parts())

# 并发删除全部 10 个条目：恰好一次回收
ids = [it4["id"], octet_item["id"]] + [o["id"] for o in outs]
with ThreadPoolExecutor(8) as ex:
    ds = list(ex.map(M.delete_item, ids))
check("并发删除恰好回收 1 次实体",
      sum(1 for d in ds if d["entity_reclaimed"]) == 1)
check("实体文件已删、实体行清零",
      not M.entity_path(sha4, ".mp3").exists())

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
