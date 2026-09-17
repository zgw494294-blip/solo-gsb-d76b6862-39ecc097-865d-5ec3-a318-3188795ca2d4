"""提示素材库端到端测试（不依赖 FastAPI / 网络，直接驱动 assets 核心）。

用自建的 multipart 请求桩逐块（随机分块以压测边界跨块）喂给流式上传逻辑，
验证：扩展名 / MIME / 大小校验、SHA-256 去重、原子落盘、引用保护、删除最后
条目才回收实体、完整性检查、并发重复上传不留临时文件 / 空记录。
"""
import asyncio
import hashlib
import os
import random
import sys
import tempfile
import threading
import types

# --- stub fastapi.HTTPException（assets.py 仅用到它） ---
fastapi = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


fastapi.HTTPException = HTTPException
sys.modules["fastapi"] = fastapi

tmpdir = tempfile.mkdtemp()
os.environ["CUE_DB_PATH"] = os.path.join(tmpdir, "assets.db")
os.environ["SEED_DEMO"] = "0"
os.environ["ASSET_MAX_SIZE"] = str(4096)  # 压测大小上限
sys.path.insert(0, os.path.dirname(__file__))

from app import assets, database  # noqa: E402

database.init_db()
database.seed_demo(force=True)  # 提供 8 条演示提示用于绑定；演示素材随后会被清掉
assets.ensure_storage()

# 清空种子素材以获得干净的素材计数（提示保留）
with database.db() as _c:
    _c.execute("DELETE FROM run_cue_assets")
    _c.execute("DELETE FROM cue_assets")
    _c.execute("DELETE FROM assets")
    _c.execute("DELETE FROM asset_blobs")
    _c.commit()
import shutil
shutil.rmtree(database.BLOB_DIR, ignore_errors=True)
assets.ensure_storage()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def expect_status(name, status, fn):
    try:
        fn()
        check(name, False, "（未抛异常）")
    except HTTPException as e:
        check(f"{name} -> {status}", e.status_code == status,
              f"got {e.status_code}")
    except Exception as e:  # noqa
        check(name, False, f"（{type(e).__name__}: {e}）")


# ------------------------------------------------------------ 请求桩

class FakeRequest:
    def __init__(self, body: bytes, content_type: str):
        self.headers = {"content-type": content_type}
        self._body = body

    async def stream(self):
        # 随机分块，且大量 1 字节块，压测边界跨块 / marker 半匹配
        i = 0
        while i < len(self._body):
            step = random.choice([1, 1, 2, 3, 5, 7, 64, 128])
            yield self._body[i:i + step]
            i += step


def encode_multipart(fields, files, boundary="----testboundary123"):
    """fields: [(name, value)]; files: [(name, filename, content)]，保持顺序。"""
    out = bytearray()
    for name, value in fields:
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for name, filename, content in files:
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n').encode()
        out += b"Content-Type: application/octet-stream\r\n\r\n"
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def build_body(filename, content, name="", name_first=False,
               boundary="----testboundary123"):
    """显式构造 name 在前 / 在后两种部件顺序。"""
    out = bytearray()

    def field():
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(b'Content-Disposition: form-data; name="name"\r\n\r\n')
        out.extend(name.encode() + b"\r\n")

    def filepart():
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'.encode())
        out.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        out.extend(content + b"\r\n")

    if name_first and name:
        field()
    filepart()
    if not name_first and name:
        field()
    out.extend(f"--{boundary}--\r\n".encode())
    return bytes(out), f"multipart/form-data; boundary={boundary}"


PNG = (b"\x89PNG\r\n\x1a\n" +
       b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + bytes([8, 2, 0, 0, 0]))
# 追加随机负载使不同文件哈希不同，并保证长度
PNG_A = PNG + os.urandom(200) + b"IEND\xaeB`\x82"
PNG_B = PNG + os.urandom(200) + b"IEND\xaeB`\x82"
WAV = (b"RIFF" + (40).to_bytes(4, "little") + b"WAVEfmt " +
       (16).to_bytes(4, "little") + (1).to_bytes(2, "little") +
       (1).to_bytes(2, "little") + b"\x00" * 20 + b"data")


def make_wav(secs=0.05, rate=8000, freq=440):
    import math
    import struct
    n = int(rate * secs)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
                   for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) +
            b"data" + struct.pack("<I", len(pcm)) + pcm)


def upload(content, filename, name="", fields_first=False):
    body, ctype = build_body(filename, content, name,
                            name_first=fields_first)
    req = FakeRequest(body, ctype)
    return asyncio.run(assets.create_asset(req))


print("== 1. 正常上传（图片 / 音频） ==")
random.seed(1)
a1 = upload(PNG_A, "pic.png", name="灯光示意图")
check("图片上传 201 并返回就绪", a1["state"] == "ready" and a1["kind"] == "image")
check("名称取自 name 字段", a1["name"] == "灯光示意图", a1["name"])
check("实体文件存在", assets.blob_path(a1["sha256"], a1["ext"]).exists())
wav = make_wav()
a2 = upload(wav, "tone.wav", name="提示音")
check("音频上传", a2["media_type"] == "audio/wav" and a2["state"] == "ready")

print("== 2. 字段顺序 / 随机分块（name 在文件之后） ==")
a3 = upload(PNG_B, "b.png", name="文件在前的名称", fields_first=False)
check("文件在前 + name 在后仍正确", a3["name"] == "文件在前的名称")
check("内容未被边界污染（哈希正确）",
      a3["sha256"] == hashlib.sha256(PNG_B).hexdigest())
# name 字段在文件之前（常规浏览器顺序）
PNG_C = PNG + os.urandom(150) + b"NAMEFIRST"
a3b = upload(PNG_C, "c.png", name="名称在前", fields_first=True)
check("name 在文件之前也正确", a3b["name"] == "名称在前" and
      a3b["sha256"] == hashlib.sha256(PNG_C).hexdigest())
# 未提供 name 时回退为文件名（去扩展名）。multipart 文件名按 latin-1 解析，
# 浏览器对非 ASCII 文件名会通过独立的 name 字段传名，故这里用 ASCII 文件名。
a3c = upload(PNG_C, "fallback-name.png")
check("缺省 name 回退为文件名", a3c["name"] == "fallback-name", a3c["name"])
# 无 name 的再次上传不报错且复用实体
check("第三次同内容仍就绪", a3c["state"] == "ready")

print("== 3. 去重：相同哈希多个条目，实体仅一份 ==")
a4 = upload(PNG_A, "copy.png", name="同图不同名")
check("相同哈希建立第二个条目", a4["id"] != a1["id"] and a4["sha256"] == a1["sha256"])
with database.db() as conn:
    n_blob = conn.execute("SELECT COUNT(*) c FROM asset_blobs WHERE sha256=?",
                          (a1["sha256"],)).fetchone()["c"]
    n_entry = conn.execute("SELECT COUNT(*) c FROM assets WHERE sha256=?",
                           (a1["sha256"],)).fetchone()["c"]
check("实体只有一份", n_blob == 1, n_blob)
check("条目有两个", n_entry == 2, n_entry)

print("== 4. 校验失败不留临时文件 / 空记录 ==")
before_tmp = len(os.listdir(database.TMP_DIR))
before_entries = len(assets.list_assets()["items"])
expect_status("伪造扩展名（exe 内容叫 .png）-> 415", 415,
              lambda: upload(b"MZ\x90\x00\x03\x00binary", "evil.png"))
expect_status("不支持的扩展名 .exe -> 415", 415,
              lambda: upload(b"MZ", "payload.exe"))
expect_status("空文件 -> 400", 400, lambda: upload(b"", "empty.png"))


def no_file():
    body, ctype = build_body("", b"", name="x")  # 只有 name，没有 file 部件
    # build_body 总会放文件部件，这里手工构造仅含 name 的请求
    b = "----testboundary123"
    raw = (f"--{b}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n"
           f"x\r\n--{b}--\r\n").encode()
    return asyncio.run(assets.create_asset(
        FakeRequest(raw, f"multipart/form-data; boundary={b}")))


expect_status("无文件字段 -> 400", 400, no_file)
# 超大文件
big = PNG + os.urandom(9000)
expect_status("超过大小上限 -> 413", 413, lambda: upload(big, "big.png"))
after_tmp = len(os.listdir(database.TMP_DIR))
after_entries = len(assets.list_assets()["items"])
check("失败后无残留临时文件", after_tmp == before_tmp, f"{before_tmp}->{after_tmp}")
check("失败后无空记录", after_entries == before_entries,
      f"{before_entries}->{after_entries}")

print("== 5. 绑定与引用保护 ==")
assets.set_cue_assets(1, [a1["id"]])
expect_status("删除被引用条目 -> 409", 409, lambda: assets.delete_asset(a1["id"]))
# 共享同一实体的另一条目 a4 未被引用 -> 可删除，实体必须保留
r = assets.delete_asset(a4["id"])
check("删除共享实体的非引用条目成功", r["deleted"] and r["blob_reclaimed"] is False)
check("共享实体未被误删", assets.blob_path(a1["sha256"], a1["ext"]).exists())
# 解绑后删除 a1 -> 最后一个同哈希条目 -> 回收实体
assets.set_cue_assets(1, [])
r = assets.delete_asset(a1["id"])
check("删除最后一个条目回收实体", r["blob_reclaimed"] is True)
check("实体文件已回收", not assets.blob_path(a1["sha256"], a1["ext"]).exists())
with database.db() as conn:
    gone = conn.execute("SELECT COUNT(*) c FROM asset_blobs WHERE sha256=?",
                        (a1["sha256"],)).fetchone()["c"]
check("blob 行已删除", gone == 0)

print("== 6. 完整性检查：缺失 / 哈希不符 -> 所有引用提示未就绪 ==")
assets.set_cue_assets(2, [a2["id"]])
p = assets.blob_path(a2["sha256"], a2["ext"])
# 6a. 篡改内容（保留大小外的字节 -> 哈希不符）
orig_bytes = p.read_bytes()
p.write_bytes(b"X" + orig_bytes[1:])
rep = assets.verify_integrity()
check("检出哈希不符", any(m["sha256"] == a2["sha256"] for m in rep["mismatch"]),
      str(rep["mismatch"]))
check("受影响提示含 #2", 2 in rep["affected_cue_ids"], rep["affected_cue_ids"])
info = assets.get_asset(a2["id"])
check("素材状态未就绪", info["state"] == "not_ready")
with database.db() as conn:
    amap = assets.cue_asset_map(conn)
check("提示 #2 素材未就绪",
      any(x["state"] == "not_ready" for x in amap.get(2, [])))
# 恢复后重新校验 -> 恢复就绪
p.write_bytes(orig_bytes)
rep2 = assets.verify_integrity()
check("恢复后校验健康", rep2["healthy"] and assets.get_asset(a2["id"])["state"] == "ready")
# 6b. 删除实体文件 -> missing
p.unlink()
rep3 = assets.verify_integrity()
check("检出实体缺失", any(m["sha256"] == a2["sha256"] for m in rep3["missing"]))
expect_status("未就绪实体的内容接口 -> 409", 409,
              lambda: assets.get_asset_for_content(a2["id"]))
# 放回文件恢复（演示数据恢复路径）
p.parent.mkdir(parents=True, exist_ok=True)
p.write_bytes(orig_bytes)
assets.verify_integrity()

print("== 7. 并发重复上传：只一份实体 / 一条 blob，无临时残留 ==")
same = PNG + os.urandom(123) + b"PARALLEL"
results, errors = [], []


def worker():
    try:
        results.append(upload(same, f"t{threading.get_ident()}.png", name="并发"))
    except Exception as e:  # noqa
        errors.append(e)


threads = [threading.Thread(target=worker) for _ in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("6 个并发上传全部成功", len(results) == 6 and not errors,
      f"{len(results)} ok, {errors[:1]}")
sha_same = hashlib.sha256(same).hexdigest()
with database.db() as conn:
    nb = conn.execute("SELECT COUNT(*) c FROM asset_blobs WHERE sha256=?",
                      (sha_same,)).fetchone()["c"]
    ne = conn.execute("SELECT COUNT(*) c FROM assets WHERE sha256=?",
                      (sha_same,)).fetchone()["c"]
check("并发后实体仅一份", nb == 1, nb)
check("并发后条目 6 个", ne == 6, ne)
check("并发后无临时文件残留", len(os.listdir(database.TMP_DIR)) == 0)
check("并发后实体文件存在且内容一致",
      assets.blob_path(sha_same, ".png").read_bytes() == same)

print("== 8. 按名称检索 ==")
res = assets.list_assets("提示音")
check("名称检索命中「提示音」", len(res["items"]) >= 1 and
      all("提示音" in x["name"] for x in res["items"]),
      f"{res['count']} hits")
res2 = assets.list_assets("不存在的素材名xyz")
check("检索无结果", res2["items"] == [])
check("空检索返回全部", assets.list_assets("")["count"] >= 6)

print("== 9. 演示数据种子（图片+音频，绑定前两条提示） ==")
database.seed_demo(force=True)
with database.db() as conn:
    na = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
    # 取当前排序前两条提示的真实 id
    first_two = [r["id"] for r in conn.execute(
        "SELECT id FROM cues ORDER BY sort_order, id LIMIT 2").fetchall()]
    bound = conn.execute(
        f"SELECT COUNT(*) c FROM cue_assets WHERE cue_id IN "
        f"({','.join('?' for _ in first_two)})", first_two).fetchone()["c"]
check("演示素材写入", na >= 2, na)
check("演示素材绑定到前两条提示", bound == 2, f"bound={bound}, ids={first_two}")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
