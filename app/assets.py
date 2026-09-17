"""提示素材库核心。

设计要点
--------
* **流式上传**：不依赖任何 multipart 解析库，逐块从请求体读取，边写临时文件边
  计算 SHA-256；任何时刻内存与磁盘占用都受 ``ASSET_MAX_SIZE`` 限制，超限立即中断。
* **三重校验**：扩展名白名单 + 魔数嗅探的真实类型 + 大小上限；扩展名与真实类型
  不一致同样拒绝（如把可执行文件改名为 .png）。
* **原子落盘**：先写入临时目录（与实体目录同一文件系统），全部通过后在
  ``BEGIN IMMEDIATE`` 事务内 ``os.replace`` 原子移入内容寻址路径
  ``blobs/<sha前2位>/<sha>.<ext>``，再提交事务。
* **按哈希去重**：相同内容只保存一份实体（asset_blobs），但每次上传都建立独立
  素材条目（assets）。
* **引用保护**：``cue_assets.asset_id`` 外键 ``ON DELETE RESTRICT``，被提示引用的
  条目删除直接 409；只有删除某哈希的**最后一个**条目时才回收实体文件，且在事务
  提交成功后才真正删文件，因此回滚 / 并发重复上传绝不会误删共享实体。
* **失败不留痕**：校验失败、解析失败、数据库回滚都会删除临时文件；实体替换失败
  时在仍持有写锁的情况下回滚并清理本次刚放入的文件，共享实体始终安全。
"""
from __future__ import annotations

import hashlib
import os
import urllib.parse
from pathlib import Path

from fastapi import HTTPException

from . import database

# ------------------------------------------------------------ 允许的文件类型
# 扩展名 -> 允许的真实 MIME 集合（魔数嗅探结果必须落在其中）。
ALLOWED_TYPES: dict[str, set[str]] = {
    # 图片
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".gif": {"image/gif"},
    ".webp": {"image/webp"},
    # 音频
    ".mp3": {"audio/mpeg"},
    ".wav": {"audio/wav", "audio/x-wav"},
    ".ogg": {"audio/ogg"},
    ".flac": {"audio/flac", "audio/x-flac"},
    ".m4a": {"audio/mp4", "audio/x-m4a"},
    ".aac": {"audio/aac"},
    # 视频
    ".mp4": {"video/mp4"},
    ".mov": {"video/quicktime"},
    ".webm": {"video/webm", "audio/webm"},
    ".mkv": {"video/x-matroska"},
    ".avi": {"video/x-msvideo"},
}

MAX_UPLOAD_SIZE = int(os.environ.get("ASSET_MAX_SIZE", 200 * 1024 * 1024))
CHUNK_SIZE = 64 * 1024

READY = "ready"
NOT_READY = "not_ready"


class UploadError(ValueError):
    """上传解析失败（区别于业务校验，调用方据此返回 400）。"""


# ------------------------------------------------------------ 存储路径

def ensure_storage() -> None:
    """创建持久化目录并清理上次崩溃可能残留的临时文件（启动时调用）。"""
    Path(database.BLOB_DIR).mkdir(parents=True, exist_ok=True)
    tmp = Path(database.TMP_DIR)
    tmp.mkdir(parents=True, exist_ok=True)
    for child in tmp.iterdir():
        # 每次上传使用独立子目录；残留项（子目录或散落文件）一律清除
        import shutil
        if child.is_symlink() or child.is_file():
            _safe_unlink(child)
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def blob_path(sha256: str, ext: str) -> Path:
    return Path(database.BLOB_DIR) / sha256[:2] / f"{sha256}{ext}"


def _safe_unlink(path: Path | str | None) -> None:
    if path is None:
        return
    try:
        p = Path(path)
        if p.is_symlink() or p.is_file():
            p.unlink()
    except OSError:
        pass


# ------------------------------------------------------------ 魔数嗅探（纯标准库）

def sniff_media_type(head: bytes) -> str | None:
    """根据文件头魔数判断真实媒体类型；无法识别返回 None。"""
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    if len(head) >= 4 and head[:4] == b"fLaC":
        return "audio/flac"
    if len(head) >= 4 and head[:4] == b"OggS":
        # Ogg 容器（Vorbis/Opus 音频）
        return "audio/ogg"
    if len(head) >= 2 and head[:2] == b"\x1a\x45":  # EBFL/EBML：webm / matroska
        return "video/x-matroska"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if len(head) >= 3 and (head[:3] == b"ID3" or head[:2] in
                           (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa")):
        return "audio/mpeg"
    return None


def _ebml_doctype(head: bytes, ext: str) -> str:
    """读取 EBML 头区分 webm / matroska；读不到时按扩展名归类。"""
    if b"webm" in head[:8192]:
        return "video/webm"
    if b"matroska" in head[:8192]:
        return "video/x-matroska"
    return "video/webm" if ext == ".webm" else "video/x-matroska"


def detect_type(head: bytes, ext: str) -> str | None:
    mt = sniff_media_type(head)
    if mt == "video/x-matroska":
        return _ebml_doctype(head, ext)
    if mt is None and len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        # 裸 ADTS AAC（0xFFF0/0xFFF1/0xFFF8/0xFFF9）
        return "audio/aac" if ext == ".aac" else "audio/mpeg"
    return mt


# ------------------------------------------------------------ 流式 multipart 解析

class _Part:
    """multipart 的一个部件，提供流式 readchunk()。"""

    def __init__(self, parser: "_MultipartParser", headers: dict[str, str]):
        self._p = parser
        self.headers = headers
        disp = headers.get("content-disposition", "")
        self.name = None
        self.filename = None
        filename_star = None
        for token in disp.split(";"):
            token = token.strip()
            if token.startswith("name="):
                self.name = token[5:].strip().strip('"')
            elif token.lower().startswith("filename*="):
                # RFC 5987: filename*=UTF-8''%e5…（优先于 filename）
                value = token.split("=", 1)[1].strip().strip('"')
                if "'" in value:
                    value = value.split("'", 2)[2]
                filename_star = urllib.parse.unquote(value, encoding="utf-8")
            elif token.lower().startswith("filename="):
                self.filename = token.split("=", 1)[1].strip().strip('"')
        if filename_star is not None:
            self.filename = filename_star

    async def readchunk(self) -> bytes:
        """返回下一块数据；b'' 表示该部件结束（边界已消费）。"""
        return await self._p.read_part_chunk()

    async def readall(self, max_size: int) -> bytes:
        buf = bytearray()
        while True:
            chunk = await self.readchunk()
            if not chunk:
                return bytes(buf)
            if len(buf) + len(chunk) > max_size:
                raise UploadError("文本字段超过长度上限")
            buf.extend(chunk)


class _MultipartParser:
    r"""极简的流式 multipart/form-data 解析器。

    维护一块来自请求体的缓冲，逐块在其中查找 ``\r\n--<boundary>``；缓冲不足以
    判断时保留尾部 ``len(marker)-1`` 字节继续读，因此边界即使被任意分块切开也能
    正确识别。文件内容逐块交给上层写盘，内存占用恒定。
    """

    def __init__(self, stream, boundary: bytes):
        self._stream = stream
        self._boundary = b"--" + boundary
        self._marker = b"\r\n" + self._boundary
        self._buf = b""
        self._eof = False
        self._done = False       # 整个 multipart 结束
        self._part_done = False  # 当前部件内容已读完（边界已消费）
        self._at_headers = False  # 缓冲当前定位在下一部件的头部（而非边界）

    async def _fill(self) -> bool:
        if self._eof:
            return False
        async for chunk in self._stream:
            if chunk:
                self._buf += chunk
                return True
        self._eof = True
        return False

    async def _readline(self, limit: int = 64 * 1024) -> bytes:
        while b"\r\n" not in self._buf:
            if len(self._buf) > limit:
                raise UploadError("multipart 部件头部过长")
            if not await self._fill():
                raise UploadError("multipart 请求体不完整（缺少换行）")
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    async def _consume_boundary_suffix(self) -> None:
        """消费边界后的 CRLF（还有部件）或 '--'（结束）。"""
        while len(self._buf) < 2:
            if not await self._fill():
                raise UploadError("multipart 请求体在边界处截断")
        if self._buf[:2] == b"--":
            self._buf = self._buf[2:]
            self._done = True
        elif self._buf[:2] == b"\r\n":
            self._buf = self._buf[2:]
        else:
            raise UploadError("multipart 边界格式非法")

    async def next_part(self) -> _Part | None:
        """定位到下一个部件并返回；所有部件结束返回 None。

        首次调用时缓冲位于起始边界；消费完上一个部件的内容后，缓冲已越过边界、
        定位在新部件的头部（``_at_headers``），直接读取即可。
        """
        if self._done:
            return None
        if not self._at_headers:
            if not self._buf:
                if not await self._fill():
                    raise UploadError("空的 multipart 请求体")
            if not self._buf.startswith(self._boundary):
                idx = self._buf.find(self._boundary)
                while idx < 0:
                    if not await self._fill():
                        raise UploadError("multipart 请求体缺少起始边界")
                    idx = self._buf.find(self._boundary)
                self._buf = self._buf[idx + len(self._boundary):]
            else:
                self._buf = self._buf[len(self._boundary):]
            await self._consume_boundary_suffix()
        self._at_headers = False
        self._part_done = self._done
        if self._done:
            return None
        # 读部件头部
        headers: dict[str, str] = {}
        while True:
            line = await self._readline()
            if line == b"":
                break
            try:
                k, v = line.decode("latin1").split(":", 1)
            except ValueError:
                raise UploadError("multipart 部件头部非法")
            headers[k.strip().lower()] = v.strip()
        return _Part(self, headers)

    async def read_part_chunk(self) -> bytes:
        """读取当前部件内容，直到遇到下一个边界。

        边界在本调用内被消费，缓冲随之定位到下一部件头部；再调用一次返回
        ``b""`` 表示本部件结束。
        """
        if self._part_done:
            return b""
        keep = len(self._marker) - 1
        while True:
            idx = self._buf.find(self._marker)
            if idx >= 0:
                data = self._buf[:idx]
                self._buf = self._buf[idx + len(self._marker):]
                await self._consume_boundary_suffix()
                # 越界后缓冲位于下一部件头部（若非结束）
                self._at_headers = not self._done
                self._part_done = True
                return data
            # 未找到：安全吐出除尾部 keep（可能是半个 marker）外的字节
            if len(self._buf) > keep:
                safe = self._buf[:-keep]
                self._buf = self._buf[len(safe):]
                return safe
            if self._eof:
                raise UploadError("multipart 请求体不完整（缺少结束边界）")
            await self._fill()


def _boundary_from(request) -> bytes:
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise UploadError("Content-Type 必须是 multipart/form-data")
    params = ctype.split(";", 1)[1] if ";" in ctype else ""
    for token in params.split(";"):
        token = token.strip()
        if token.lower().startswith("boundary="):
            value = token[9:].strip().strip('"')
            if value:
                return value.encode("latin1")
    raise UploadError("multipart 请求缺少 boundary")


# ------------------------------------------------------------ 上传落盘

def _split_ext(filename: str) -> str:
    return os.path.splitext(urllib.parse.unquote(filename or ""))[1].lower()[:16]


def _new_tmp_file():
    """原子地创建一个独占的临时文件（每个上传使用独立子目录），
    返回 (路径, 已打开的写入句柄)。

    * 每个上传在 tmp 下拥有 uuid 命名的**独立子目录**，互不共享任何目录项；
    * O_CREAT|O_EXCL 保证同名绝不覆盖。
    """
    import uuid
    for _ in range(8):
        sub = Path(database.TMP_DIR) / uuid.uuid4().hex
        try:
            sub.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        candidate = sub / "upload.part"
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return candidate, os.fdopen(fd, "wb")
    raise OSError("无法分配唯一的临时文件目录")


def _remove_tmp_tree(tmp: Path | None) -> None:
    """删除某次上传独占的临时子目录（绝不触碰其它上传的目录）。"""
    if tmp is None:
        return
    import shutil
    shutil.rmtree(tmp.parent, ignore_errors=True)


def _materialize(tmp: Path, target: Path) -> bool:
    """把**自己的**临时文件物化为内容寻址共享实体 ``target``。

    并发重复上传内容相同（同一 sha -> 同一 target）时不能用 ``os.replace``：
    后到者会把先到者刚移入的 inode 解绑。这里用**硬链接**原子地在 target 新建
    目录项（自己的临时文件 inode 保持），随后调用方只解绑自己的临时文件，共享
    target 的链接数始终 ≥ 1，绝不误删。硬链接不可用时退回 copyfile。

    返回 True 表示 target 已就绪（本次创建或已存在）。``os.link`` 与前置
    ``exists`` 检查之间存在天然竞态：并发者可能恰在间隙发布 target 或自己的临时
    文件已被清理，因此把「target 已存在」一律视为成功，不抛异常。
    """
    if target.exists():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(tmp, target)
        return True
    except FileExistsError:
        return True  # 并发者已发布，内容相同
    except FileNotFoundError:
        return target.exists()
    except OSError:
        import shutil
        if target.exists():
            return True
        try:
            shutil.copyfile(tmp, target)
            return True
        except FileExistsError:
            return True
        except FileNotFoundError:
            return target.exists()


def _place_blob(conn, tmp: Path, sha: str, ext: str, size: int,
                media_type: str) -> None:
    """在已打开的写事务内物化实体并登记 blob（调用方随后提交）。

    全程只操作**自己的**临时文件，绝不删除共享 target：

    * blob 已存在（重复 / 并发上传，持有写锁时视图确定）：直接复用，由调用方
      清理自己的临时文件。
    * blob 不存在：先硬链接物化实体，再插入 blob 行；若插入时唯一约束失败，
      说明并发者已先提交（内容相同），共享 target 已存在，无需回收。
    """
    import sqlite3

    row = conn.execute("SELECT sha256 FROM asset_blobs WHERE sha256 = ?",
                       (sha,)).fetchone()
    target = blob_path(sha, ext)
    if row is not None:
        # 实体已登记：仅清理自己的临时文件，绝不触碰共享 target。
        _remove_tmp_tree(tmp)
        return

    # 提交前物化：事务一旦可见，实体文件必已就位。并发下多个相同内容请求会
    # 先后尝试在同一 target 建硬链接；已存在即视为成功（内容寻址，字节相同）。
    if not _materialize(tmp, target):
        # 自己的临时文件缺失且 target 也不存在：异常状态，整体失败并回滚。
        raise HTTPException(500, "临时文件在落盘前丢失，请重试上传")
    _remove_tmp_tree(tmp)  # 只删除自己的临时目录；target 是共享实体，绝不删除
    try:
        conn.execute(
            "INSERT INTO asset_blobs (sha256, ext, size, media_type,"
            " verify_status, verified_at) VALUES (?, ?, ?, ?, 'ok',"
            " datetime('now'))",
            (sha, ext, size, media_type))
    except sqlite3.IntegrityError:
        # 并发者已先登记该 sha：实体内容相同，共享 target 保留。
        return


async def create_asset(request) -> dict:
    """流式接收 multipart 上传：边读边写临时文件并计算 SHA-256，随后三重校验、
    原子去重落盘、建立素材条目。任何失败都清理临时文件，不写空记录。"""
    # 注意：这里不能调用 ensure_storage()——它会清理整个 tmp 目录，
    # 高并发下会误删其它正在写入的临时文件。目录在启动时已建好。
    Path(database.TMP_DIR).mkdir(parents=True, exist_ok=True)
    try:
        boundary = _boundary_from(request)
    except UploadError as exc:
        raise HTTPException(400, f"上传解析失败：{exc}")

    parser = _MultipartParser(request.stream(), boundary)
    name_override = ""
    tmp: Path | None = None
    hasher = hashlib.sha256()
    total = 0
    head = b""
    file_ext = ""
    original = ""
    try:
        while True:
            part = await parser.next_part()
            if part is None:
                break
            if part.filename is not None:
                if tmp is not None:
                    raise HTTPException(400, "一次只能上传一个文件")
                fname = part.filename or ""
                if len(fname) > 240:
                    raise HTTPException(400, "文件名过长")
                file_ext = _split_ext(fname)
                original = os.path.basename(fname)
                if file_ext not in ALLOWED_TYPES:
                    raise HTTPException(
                        415,
                        f"不支持的文件类型「{file_ext or '无扩展名'}」，"
                        "仅允许图片 / 音频 / 视频")
                tmp, fh = _new_tmp_file()
                # 边接收边写临时文件 + 计算哈希（保持当前部件活跃，边界后
                # 的下一部件仍能被解析）
                try:
                    while True:
                        try:
                            chunk = await part.readchunk()
                        except UploadError as exc:
                            raise HTTPException(400, f"上传解析失败：{exc}")
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UPLOAD_SIZE:
                            raise HTTPException(
                                413, f"文件超过大小上限 "
                                     f"{MAX_UPLOAD_SIZE // (1024 * 1024)}MB")
                        if len(head) < 8192:
                            head += chunk[:8192 - len(head)]
                        hasher.update(chunk)
                        fh.write(chunk)
                finally:
                    # 部件体读完即彻底落盘并关闭，确保后续 os.link 可见
                    fh.flush()
                    os.fsync(fh.fileno())
                    fh.close()
            elif part.name == "name":
                name_override = (
                    await part.readall(1024)).decode("utf-8", "replace").strip()
            elif part.name is not None:
                await part.readall(64 * 1024)  # 其余文本字段读出丢弃
    except HTTPException:
        _remove_tmp_tree(tmp)
        raise
    except UploadError as exc:
        _remove_tmp_tree(tmp)
        raise HTTPException(400, f"上传解析失败：{exc}")
    except Exception:
        # 任何其它解析异常也不能留下临时文件 / 空记录
        _remove_tmp_tree(tmp)
        raise

    if tmp is None:
        raise HTTPException(400, "缺少文件字段（file）")
    if total == 0:
        _remove_tmp_tree(tmp)
        raise HTTPException(400, "文件内容为空")

    media_type = detect_type(head, file_ext)
    if media_type is None:
        _remove_tmp_tree(tmp)
        raise HTTPException(415, "无法识别文件内容（魔数与支持的图片/音频/视频不符）")
    if media_type not in ALLOWED_TYPES[file_ext]:
        _remove_tmp_tree(tmp)
        raise HTTPException(
            415, f"文件内容（{media_type}）与扩展名「{file_ext}」不匹配，已拒绝")

    name = name_override or os.path.splitext(original)[0]
    name = (name.strip() or f"素材-{hasher.hexdigest()[:8]}")[:120]
    sha = hasher.hexdigest()

    with database.db() as conn:
        asset_id = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 持写锁期间：物化实体（硬链接，只动自己的临时文件）+ 登记 blob + 条目，
            # 随后一起提交；因此事务对外可见时实体文件必然已就位。
            _place_blob(conn, tmp, sha, file_ext, total, media_type)
            cur = conn.execute(
                "INSERT INTO assets (name, original_name, ext, media_type,"
                " size, sha256) VALUES (?, ?, ?, ?, ?, ?)",
                (name, original[:240], file_ext, media_type, total, sha))
            asset_id = cur.lastrowid
            conn.commit()
        except BaseException:
            conn.rollback()
            _remove_tmp_tree(tmp)
            raise

    # 提交成功：解绑自己的临时文件（硬链接后共享实体另有独立目录项，不受影响）。
    _remove_tmp_tree(tmp)
    return get_asset(asset_id)


# ------------------------------------------------------------ 查询

def _blob_states(conn, shas: list[str]) -> dict[str, str]:
    """批量判断实体就绪状态：存在且大小一致、且上次完整校验非 bad/missing。"""
    result: dict[str, str] = {}
    for sha in set(shas):
        row = conn.execute(
            "SELECT ext, size, verify_status FROM asset_blobs WHERE sha256 = ?",
            (sha,)).fetchone()
        if row is None:
            result[sha] = NOT_READY
            continue
        path = blob_path(sha, row["ext"])
        ok = path.exists() and path.stat().st_size == row["size"]
        result[sha] = READY if (ok and row["verify_status"] == "ok") else NOT_READY
    return result


def _asset_payload(row, state: str, cue_ids: list[int]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "original_name": row["original_name"],
        "ext": row["ext"],
        "media_type": row["media_type"],
        "kind": row["media_type"].split("/", 1)[0],
        "size": row["size"],
        "sha256": row["sha256"],
        "state": state,
        "content_url": f"/api/assets/{row['id']}/content",
        "cue_ids": cue_ids,
        "created_at": row["created_at"],
    }


def _serialize_asset(conn, row) -> dict:
    state = _blob_states(conn, [row["sha256"]])[row["sha256"]]
    refs = conn.execute(
        "SELECT cue_id FROM cue_assets WHERE asset_id = ? ORDER BY cue_id",
        (row["id"],)).fetchall()
    return _asset_payload(row, state, [r["cue_id"] for r in refs])


def list_assets(q: str = "") -> dict:
    q = (q or "").strip()
    with database.db() as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM assets WHERE name LIKE ? OR original_name LIKE ?"
                " ORDER BY id DESC", (like, like)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM assets ORDER BY id DESC").fetchall()
        items = [_serialize_asset(conn, r) for r in rows]
        total = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
    return {"items": items, "count": len(items), "total": total, "query": q}


def get_asset(asset_id: int) -> dict:
    with database.db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?",
                           (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "素材不存在")
        return _serialize_asset(conn, row)


def get_asset_for_content(asset_id: int):
    """返回 (磁盘路径, media_type, 下载文件名) 供文件响应；未就绪 409。"""
    with database.db() as conn:
        row = conn.execute(
            "SELECT a.id, a.name, a.ext, a.media_type, a.sha256, b.size"
            " FROM assets a JOIN asset_blobs b ON b.sha256 = a.sha256"
            " WHERE a.id = ?", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "素材不存在")
        path = blob_path(row["sha256"], row["ext"])
        if not path.exists() or path.stat().st_size != row["size"]:
            raise HTTPException(409, "素材实体缺失或已损坏（未就绪）")
        return path, row["media_type"], row["name"] + row["ext"]


# ------------------------------------------------------------ 删除（引用保护 + 实体回收）

def delete_asset(asset_id: int) -> dict:
    with database.db() as conn:
        row = conn.execute("SELECT id, sha256 FROM assets WHERE id = ?",
                           (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "素材不存在")
        refs = conn.execute(
            "SELECT cue_id FROM cue_assets WHERE asset_id = ? ORDER BY cue_id",
            (asset_id,)).fetchall()
        if refs:
            ids = ", ".join(f"#{r['cue_id']}" for r in refs[:10])
            more = "…" if len(refs) > 10 else ""
            raise HTTPException(
                409, f"素材被 {len(refs)} 条提示引用（{ids}{more}），"
                     "请先解除绑定后再删除；被引用的素材条目不可删除")
        sha = row["sha256"]
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            # 删除最后一个同哈希条目时才回收实体
            remaining = conn.execute(
                "SELECT COUNT(*) c FROM assets WHERE sha256 = ?",
                (sha,)).fetchone()
            ext_row = conn.execute(
                "SELECT ext FROM asset_blobs WHERE sha256 = ?", (sha,)).fetchone()
            reclaim = remaining["c"] == 0 and ext_row is not None
            reclaimed_path = blob_path(sha, ext_row["ext"]) if reclaim else None
            if reclaim:
                conn.execute("DELETE FROM asset_blobs WHERE sha256 = ?", (sha,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    # 事务提交成功后才回收实体文件；回滚走不到这里，绝不会误删共享实体
    if reclaim and reclaimed_path is not None:
        _safe_unlink(reclaimed_path)
        try:
            reclaimed_path.parent.rmdir()  # 清理空的分片目录
        except OSError:
            pass
    return {"id": asset_id, "deleted": True, "blob_reclaimed": reclaim}


# ------------------------------------------------------------ 提示绑定

def set_cue_assets(cue_id: int, asset_ids: list[int]) -> dict:
    """全量替换某条提示的素材绑定（有序、去重）。"""
    if len(asset_ids) != len(set(asset_ids)):
        raise HTTPException(400, "绑定的素材不能重复")
    with database.db() as conn:
        if conn.execute("SELECT 1 FROM cues WHERE id = ?",
                        (cue_id,)).fetchone() is None:
            raise HTTPException(404, "提示不存在")
        try:
            conn.execute("BEGIN IMMEDIATE")
            if asset_ids:
                placeholders = ",".join("?" for _ in asset_ids)
                found = {r["id"] for r in conn.execute(
                    f"SELECT id FROM assets WHERE id IN ({placeholders})",
                    asset_ids).fetchall()}
                missing = [a for a in asset_ids if a not in found]
                if missing:
                    raise HTTPException(400, f"素材不存在：{missing}")
            conn.execute("DELETE FROM cue_assets WHERE cue_id = ?", (cue_id,))
            for pos, aid in enumerate(asset_ids):
                conn.execute(
                    "INSERT INTO cue_assets (cue_id, asset_id, position)"
                    " VALUES (?, ?, ?)", (cue_id, aid, pos))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"cue_id": cue_id, "asset_ids": asset_ids}


def cue_asset_map(conn, cue_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """cue_id -> 有序素材条目（含实时就绪状态）。供提示单 / 详情使用。"""
    sql = ("SELECT ca.cue_id, ca.position, a.id AS asset_id, a.name, a.ext,"
           " a.media_type, a.size, a.sha256 FROM cue_assets ca"
           " JOIN assets a ON a.id = ca.asset_id")
    params: tuple = ()
    if cue_ids is not None:
        if not cue_ids:
            return {}
        ph = ",".join("?" for _ in cue_ids)
        sql += f" WHERE ca.cue_id IN ({ph})"
        params = tuple(cue_ids)
    sql += " ORDER BY ca.cue_id, ca.position"
    rows = conn.execute(sql, params).fetchall()
    states = _blob_states(conn, [r["sha256"] for r in rows])
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["cue_id"], []).append({
            "id": r["asset_id"],
            "name": r["name"],
            "ext": r["ext"],
            "media_type": r["media_type"],
            "kind": r["media_type"].split("/", 1)[0],
            "size": r["size"],
            "sha256": r["sha256"],
            "state": states[r["sha256"]],
            "content_url": f"/api/assets/{r['asset_id']}/content",
        })
    return out


def freeze_run_assets(conn, run_id: int) -> None:
    """开场时把当前提示↔素材绑定冻结进 run_cue_assets（在调用方事务内）。"""
    rows = conn.execute(
        "SELECT ca.cue_id, ca.position, ca.asset_id, a.name, a.ext,"
        " a.media_type, a.size, a.sha256 FROM cue_assets ca"
        " JOIN assets a ON a.id = ca.asset_id"
        " ORDER BY ca.cue_id, ca.position").fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO run_cue_assets (run_id, cue_id, asset_id, position,"
            " name, media_type, sha256, ext, size)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, r["cue_id"], r["asset_id"], r["position"], r["name"],
             r["media_type"], r["sha256"], r["ext"], r["size"]))


def run_cue_asset_map(conn, run_id: int) -> dict[int, list[dict]]:
    """场次快照内 cue_id -> 素材（就绪状态按当前磁盘实况判定）。"""
    rows = conn.execute(
        "SELECT cue_id, position, asset_id, name, ext, media_type, size, sha256"
        " FROM run_cue_assets WHERE run_id = ? ORDER BY cue_id, position",
        (run_id,)).fetchall()
    states = _blob_states(conn, [r["sha256"] for r in rows])
    out: dict[int, list[dict]] = {}
    for r in rows:
        entry = {
            "id": r["asset_id"],
            "name": r["name"],
            "ext": r["ext"],
            "media_type": r["media_type"],
            "kind": r["media_type"].split("/", 1)[0],
            "size": r["size"],
            "sha256": r["sha256"],
            "state": states[r["sha256"]],
        }
        if r["asset_id"] is not None:
            entry["content_url"] = f"/api/assets/{r['asset_id']}/content"
        out.setdefault(r["cue_id"], []).append(entry)
    return out


# ------------------------------------------------------------ 完整性检查

def verify_integrity() -> dict:
    """对所有实体重新计算 SHA-256 并比对；同时发现磁盘孤儿文件。"""
    with database.db() as conn:
        blobs = conn.execute("SELECT * FROM asset_blobs").fetchall()
        missing, mismatch, ok_shas = [], [], []
        for b in blobs:
            sha, ext, size = b["sha256"], b["ext"], b["size"]
            path = blob_path(sha, ext)
            if not path.exists():
                missing.append({"sha256": sha, "reason": "实体文件缺失"})
                continue
            if path.stat().st_size != size:
                mismatch.append({"sha256": sha, "reason": "文件大小不符"})
                continue
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                    h.update(chunk)
            if h.hexdigest() != sha:
                mismatch.append({"sha256": sha, "reason": "SHA-256 哈希不符"})
            else:
                ok_shas.append(sha)

        # 磁盘孤儿：存在文件但数据库无对应 blob
        orphans: list[str] = []
        known = {b["sha256"] for b in blobs}
        root = Path(database.BLOB_DIR)
        if root.exists():
            for sub in root.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    if f.is_file() and (f.stem if f.suffix else f.name) not in known:
                        orphans.append(str(f.relative_to(root)))

        bad_shas = {m["sha256"] for m in missing} | {m["sha256"] for m in mismatch}
        try:
            conn.execute("BEGIN IMMEDIATE")
            for m in missing:
                conn.execute(
                    "UPDATE asset_blobs SET verify_status='missing',"
                    " verified_at=datetime('now') WHERE sha256=?", (m["sha256"],))
            for m in mismatch:
                conn.execute(
                    "UPDATE asset_blobs SET verify_status='bad',"
                    " verified_at=datetime('now') WHERE sha256=?", (m["sha256"],))
            if ok_shas:
                conn.executemany(
                    "UPDATE asset_blobs SET verify_status='ok',"
                    " verified_at=datetime('now') WHERE sha256=?",
                    [(s,) for s in ok_shas])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        affected_assets, affected_cue_ids = [], []
        if bad_shas:
            affected_assets = [
                {"id": r["id"], "name": r["name"], "sha256": r["sha256"]}
                for r in conn.execute(
                    f"SELECT id, name, sha256 FROM assets WHERE sha256 IN "
                    f"({','.join('?' for _ in bad_shas)})", tuple(bad_shas))
                .fetchall()]
            affected_cue_ids = [r["cue_id"] for r in conn.execute(
                f"SELECT DISTINCT cue_id FROM cue_assets WHERE asset_id IN "
                f"(SELECT id FROM assets WHERE sha256 IN "
                f"({','.join('?' for _ in bad_shas)}))",
                tuple(bad_shas)).fetchall()]

    return {
        "total_blobs": len(blobs),
        "ok": len(ok_shas),
        "missing": missing,
        "mismatch": mismatch,
        "orphan_files": orphans,
        "affected_assets": affected_assets,
        "affected_cue_ids": sorted(affected_cue_ids),
        "healthy": not missing and not mismatch,
    }


def cleanup_orphans() -> dict:
    """删除完整性检查发现的磁盘孤儿文件（无数据库记录的实体）。"""
    removed: list[str] = []
    root = Path(database.BLOB_DIR)
    with database.db() as conn:
        known = {r["sha256"] for r in conn.execute(
            "SELECT sha256 FROM asset_blobs").fetchall()}
    if root.exists():
        for sub in list(root.iterdir()):
            if not sub.is_dir():
                continue
            for f in list(sub.iterdir()):
                stem = f.stem if f.suffix else f.name
                if f.is_file() and stem not in known:
                    _safe_unlink(f)
                    removed.append(str(f.relative_to(root)))
            try:
                sub.rmdir()
            except OSError:
                pass
    return {"removed": removed, "count": len(removed)}


# ------------------------------------------------------------ 演示数据

def _demo_png() -> bytes:
    import base64
    # 1x1 红色像素 PNG
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP8z8DwHwAFBQIA"
        "Xf8Q1wAAAABJRU5ErkJggg==")


def _demo_wav() -> bytes:
    import io
    import math
    import struct
    # 0.25 秒 8kHz 单声道 440Hz 正弦波 PCM WAV
    rate, dur = 8000, 0.25
    n = int(rate * dur)
    pcm = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(n))
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(pcm)))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(pcm)))
    buf.write(pcm)
    return buf.getvalue()


def _store_demo_asset(name: str, data: bytes, ext: str, mt: str) -> int:
    sha = hashlib.sha256(data).hexdigest()
    with database.db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM asset_blobs WHERE sha256=?",
                                  (sha,)).fetchone()
            target = blob_path(sha, ext)
            if exists is None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(data)
                conn.execute(
                    "INSERT INTO asset_blobs (sha256, ext, size, media_type,"
                    " verify_status, verified_at) VALUES (?, ?, ?, ?, 'ok',"
                    " datetime('now'))",
                    (sha, ext, len(data), mt))
            cur = conn.execute(
                "INSERT INTO assets (name, original_name, ext, media_type,"
                " size, sha256) VALUES (?, ?, ?, ?, ?, ?)",
                (name, name + ext, ext, mt, len(data), sha))
            asset_id = cur.lastrowid
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return asset_id


def seed_demo_assets(force: bool = False, cue_ids: list[int] | None = None) -> None:
    """写入两份演示素材（图片 + 音频）并绑定到给定提示。"""
    ensure_storage()
    with database.db() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
    if existing > 0 and not force:
        return
    demos = [("开场示意图片", _demo_png(), ".png", "image/png"),
             ("序曲片段（提示音）", _demo_wav(), ".wav", "audio/wav")]
    created = [_store_demo_asset(name, data, ext, mt)
               for name, data, ext, mt in demos]
    if cue_ids:
        # force 重灌时全量替换绑定，保证演示素材稳定绑定到前两条提示
        for cue_id, asset_id in zip(cue_ids, created):
            set_cue_assets(cue_id, [asset_id])
