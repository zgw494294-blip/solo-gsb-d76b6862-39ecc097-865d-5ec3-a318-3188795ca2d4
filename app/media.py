"""提示素材库：流式上传、SHA-256 去重、原子落位与完整性校验。

三级模型
--------
* ``media_entities``：按 SHA-256 去重的**实体**，同哈希只保存一份物理文件；
* ``media_items``：素材**条目**，同一份实体可建立多个条目（不同名称 / 多次上传）；
* ``cue_media``：提示与条目的**绑定**（多对多）。

上传流程（任何一步失败都不留临时文件、不留空记录）
----------------------------------------------------
1. 先校验扩展名 / MIME / 非空文件名；
2. 以分块流式写入持久化目录内的临时文件，边写边算 SHA-256，超限即拒绝；
3. 写完后按文件头魔数复核内容类型是否与扩展名一致；
4. ``os.replace`` 原子移入内容寻址路径（同卷，崩溃也不会出现半成品实体文件）；
5. 数据库事务内 upsert 实体 + 插入条目；若实体此前处于 missing/corrupt 状态，
   本次上传即完成自愈修复。

删除与回收
----------
被任意提示引用的条目不可删除（409）；只有删除同一哈希的**最后一个条目**时，
才在同一事务内删除实体行，事务提交后才回收物理文件。共享实体（尚有其它条目
或其它上传正在进行）绝不会被误删。

并发
----
进程内对同一 SHA 加互斥锁，配合 SQLite ``BEGIN IMMEDIATE`` 写串行化，
保证并发重复上传只落一份实体文件；极端跨进程竞争也由内容寻址 + 重传自愈兜底。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import HTTPException

from . import database

# ---------------------------------------------------------------- 配置

MEDIA_ROOT = Path(os.environ.get("CUE_MEDIA_DIR", "/data/media"))
TMP_DIR = MEDIA_ROOT / "tmp"
MAX_UPLOAD_BYTES = int(os.environ.get("CUE_MEDIA_MAX_BYTES", str(200 * 1024 * 1024)))
CHUNK_SIZE = 1024 * 1024  # 1 MiB
NAME_MAX = 200

# 扩展名 -> (种类, 允许的 MIME 集合)
_EXT_TABLE: dict[str, tuple[str, frozenset[str]]] = {
    # 图片
    ".jpg":  ("image", frozenset({"image/jpeg"})),
    ".jpeg": ("image", frozenset({"image/jpeg"})),
    ".png":  ("image", frozenset({"image/png"})),
    ".gif":  ("image", frozenset({"image/gif"})),
    ".webp": ("image", frozenset({"image/webp"})),
    # 音频
    ".mp3":  ("audio", frozenset({"audio/mpeg"})),
    ".wav":  ("audio", frozenset({"audio/wav", "audio/x-wav", "audio/wave"})),
    ".ogg":  ("audio", frozenset({"audio/ogg", "application/ogg"})),
    ".m4a":  ("audio", frozenset({"audio/mp4", "audio/x-m4a", "audio/m4a"})),
    ".flac": ("audio", frozenset({"audio/flac", "audio/x-flac"})),
    # 视频
    ".mp4":  ("video", frozenset({"video/mp4"})),
    ".mov":  ("video", frozenset({"video/quicktime"})),
    ".webm": ("video", frozenset({"video/webm", "audio/webm"})),
}

ALLOWED_EXTS: frozenset[str] = frozenset(_EXT_TABLE)

# 进程内按 SHA 串行化上传 / 删除（key=sha256）
_sha_locks_guard = threading.Lock()
_sha_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _sha_lock(sha: str) -> threading.Lock:
    with _sha_locks_guard:
        return _sha_locks[sha]


# ---------------------------------------------------------------- 路径与类型

def entity_path(sha: str, ext: str) -> Path:
    """内容寻址路径：<root>/ab/abcd/<sha>.ext（两级分桶，同卷保证原子替换）。"""
    return MEDIA_ROOT / sha[:2] / sha[2:4] / f"{sha}{ext}"


def kind_of(ext: str) -> str:
    return _EXT_TABLE[ext][0]


def guess_mime(ext: str) -> str:
    mime, _ = mimetypes.guess_type(f"x{ext}")
    if mime:
        return mime
    # mimetypes 漏配时的保底
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".m4a": "audio/mp4", ".flac": "audio/flac",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    }[ext]


def init_storage() -> None:
    """创建持久化 / 临时目录（容器启动时执行）。"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 校验

_SAFE_NAME = re.compile(r"^[\w\-. 一-鿿（）()]+$")


def _split_ext(filename: str) -> str:
    """从原始文件名取小写扩展名；不合法一律 400。"""
    base = os.path.basename(filename or "")
    if not base or base.startswith(".") or "." not in base:
        raise HTTPException(400, "文件名无效：必须带扩展名（如 .mp4 / .png / .mp3）")
    ext = os.path.splitext(base)[1].lower()
    if ext not in ALLOWED_EXTS:
        allowed = " ".join(sorted(ALLOWED_EXTS))
        raise HTTPException(415, f"不支持的扩展名「{ext or '?'}」，允许：{allowed}")
    return ext


def validate_headers(filename: str, content_type: str | None) -> str:
    """落盘前校验扩展名与声明 MIME（流式读取前先拒绝明显非法请求）。"""
    ext = _split_ext(filename)
    kind, mimes = _EXT_TABLE[ext]
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        raise HTTPException(400, "缺少 Content-Type，无法识别素材类型")
    if ct not in mimes and not (
        # application/octet-stream 过于宽泛，按魔数复核时必须匹配扩展名
        ct == "application/octet-stream"
    ):
        raise HTTPException(
            415, f"MIME 类型「{ct}」与扩展名「{ext}」（{kind}）不匹配")
    return ext


def _sniff_matches(head: bytes, ext: str) -> bool:
    """按文件头魔数确认真实内容与扩展名一致。"""
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF
                                           and (head[1] & 0xE0) == 0xE0)
    if ext == ".wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".ogg":
        return head.startswith(b"OggS")
    if ext == ".flac":
        return head.startswith(b"fLaC")
    if ext == ".m4a":
        # ISO BMFF，fytp 品牌为 M4A
        return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:11] == b"M4A"
    if ext in (".mp4", ".mov"):
        # ISO BMFF 容器（mp4 / quicktime 共用 ftyp，按扩展名区分）
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if ext == ".webm":
        # Matroska / WebM EBML 头
        return len(head) >= 4 and head[:4] == b"\x1a\x45\xdf\xa3"
    return False


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "素材名称不能为空")
    if len(name) > NAME_MAX:
        raise HTTPException(400, f"素材名称不能超过 {NAME_MAX} 个字符")
    if not _SAFE_NAME.match(name):
        raise HTTPException(400, "素材名称含非法字符（仅允许中英文、数字、空格、._-（））")
    return name


# ---------------------------------------------------------------- 流式落盘

class TempSink:
    """流式写入临时文件：边写边算 SHA-256，并在超限 / 异常时清理自身。

    用法::

        sink = TempSink.open()
        try:
            async for chunk in upload_file.chunks():
                sink.feed(chunk)
            sha = sink.finish(ext)          # 魔数复核 + 原子移入
        finally:
            sink.discard()                  # 成功时为空操作
    """

    def __init__(self, path: Path):
        self.path = path
        self.fh = open(path, "wb")
        self.size = 0
        self.sha = hashlib.sha256()
        self.head = b""
        self.committed = False

    @classmethod
    def open(cls) -> "TempSink":
        init_storage()
        # os.urandom 命名避免并发 / 历史残留碰撞
        path = TMP_DIR / f"upload-{os.urandom(12).hex()}.part"
        return cls(path)

    def feed(self, data: bytes) -> None:
        if self.committed:
            return
        self.size += len(data)
        if self.size > MAX_UPLOAD_BYTES:
            limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            raise HTTPException(413, f"文件超出大小上限（{limit_mb} MB）")
        if len(self.head) < 64:
            self.head += data[:64 - len(self.head)]
        self.sha.update(data)
        self.fh.write(data)

    def finish(self, ext: str) -> tuple[str, int]:
        """关闭临时文件、做魔数复核并原子移入持久化路径。

        返回 (sha256, size)。内容寻址下同哈希即同内容，目标是否存在都执行
        原子替换：目标损坏（截断 / 哈希不符）时本次落位即完成物理修复。
        """
        self.fh.close()
        if self.size == 0:
            raise HTTPException(400, "文件为空，无法保存")
        if not _sniff_matches(self.head, ext):
            raise HTTPException(
                415, f"文件内容与扩展名「{ext}」不匹配（魔数校验失败），"
                     "可能是伪造后缀的文件")
        sha = self.sha.hexdigest()
        target = entity_path(sha, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 始终原子替换：同哈希内容必然相同，覆盖一份好文件无损；
        # 而当实体文件存在但内容损坏（截断 / 哈希不符）时，这一步即完成物理修复。
        # 同目录同卷保证原子；崩溃只会留下 tmp 残骸（启动时清理）。
        os.replace(self.path, target)
        self.committed = True
        return sha, self.size

    def discard(self) -> None:
        """清理临时文件（校验失败 / DB 回滚 / 请求中断）。成功落位后为空操作。"""
        if not self.fh.closed:
            try:
                self.fh.close()
            except OSError:
                pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- 实体就绪状态

def _set_status(conn, sha: str, status: str) -> None:
    conn.execute(
        "UPDATE media_entities SET status=?, checked_at=datetime('now') "
        "WHERE sha256=?", (status, sha))


def _check_entity(conn, ent: dict) -> dict:
    """核对单个实体的物理文件：存在性 -> 大小 -> 全量哈希。返回状态描述。"""
    path = entity_path(ent["sha256"], ent["ext"])
    if not path.exists():
        status, reason = "missing", "实体文件缺失"
    elif path.stat().st_size != ent["size_bytes"]:
        status, reason = "corrupt", "文件大小与入库记录不符"
    else:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
                h.update(block)
        if h.hexdigest() != ent["sha256"]:
            status, reason = "corrupt", "SHA-256 与入库记录不符"
        else:
            status, reason = "ok", ""
    _set_status(conn, status, ent["sha256"])
    return {"status": status, "reason": reason}


def quick_status_map(conn, shas) -> dict[str, str]:
    """列表 / 排程用的廉价就绪检查：仅查存在性与大小，不重算哈希。

    一旦文件缺失或大小变化，立即降级为 missing/corrupt；显式完整性检查才全量哈希。
    """
    out: dict[str, str] = {}
    for sha in set(shas):
        ent = conn.execute(
            "SELECT ext, size_bytes, status FROM media_entities WHERE sha256=?",
            (sha,)).fetchone()
        if ent is None:
            out[sha] = "missing"
            continue
        path = entity_path(sha, ent["ext"])
        if not path.exists():
            if ent["status"] != "missing":
                _set_status(conn, sha, "missing")
            out[sha] = "missing"
        elif path.stat().st_size != ent["size_bytes"]:
            if ent["status"] != "corrupt":
                _set_status(conn, sha, "corrupt")
            out[sha] = "corrupt"
        else:
            # 文件在位且大小一致（哈希正确性以显式完整性检查为准）；
            # 重传落位后让此前 missing/corrupt 的标记即时恢复。
            if ent["status"] in ("missing", "corrupt"):
                _set_status(conn, sha, "ok")
            out[sha] = "ok"
    conn.commit()
    return out


def attach_media_to_cues(conn, cue_ids) -> dict[int, list[dict]]:
    """为一组提示附加绑定素材（含实时就绪状态）。"""
    cue_ids = list(cue_ids)
    if not cue_ids:
        return {}
    marks = ",".join("?" for _ in cue_ids)
    rows = [dict(r) for r in conn.execute(
        f"SELECT cm.cue_id, mi.id AS item_id, mi.name, mi.original_name, "
        f"mi.mime_type, me.sha256, me.ext, me.kind, me.size_bytes "
        f"FROM cue_media cm "
        f"JOIN media_items mi ON mi.id = cm.media_item_id "
        f"JOIN media_entities me ON me.sha256 = mi.entity_sha "
        f"WHERE cm.cue_id IN ({marks}) ORDER BY cm.cue_id, mi.id",
        cue_ids).fetchall()]
    shas = {r["sha256"] for r in rows}
    statuses = quick_status_map(conn, shas) if shas else {}
    out: dict[int, list[dict]] = {cid: [] for cid in cue_ids}
    for r in rows:
        out[r["cue_id"]].append({
            "item_id": r["item_id"],
            "name": r["name"],
            "kind": r["kind"],
            "mime_type": r["mime_type"],
            "sha256": r["sha256"],
            "size_bytes": r["size_bytes"],
            "ready": statuses.get(r["sha256"], "missing") == "ok",
            "status": statuses.get(r["sha256"], "missing"),
            "url": f"/api/media/{r['item_id']}/content",
        })
    return out


def cue_ready_map(conn, cue_ids) -> dict[int, bool]:
    """提示是否所有绑定素材均就绪（无绑定视为就绪）。"""
    attached = attach_media_to_cues(conn, cue_ids)
    return {cid: all(m["ready"] for m in media)
            for cid, media in attached.items()}


# ---------------------------------------------------------------- 上传 / 删除

def commit_upload(sha: str, size: int, ext: str, mime: str,
                  name: str, original_name: str) -> dict:
    """事务内登记实体 + 条目；实体不存在或此前损坏时顺带自愈。

    返回新建条目视图。调用方必须已完成原子移入（finish）。
    """
    kind = kind_of(ext)
    with _sha_lock(sha):
        with database.db() as conn:
            try:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                ent = conn.execute(
                    "SELECT sha256, ext, status FROM media_entities "
                    "WHERE sha256=?", (sha,)).fetchone()
                if ent is None:
                    conn.execute(
                        "INSERT INTO media_entities (sha256, ext, kind, "
                        "size_bytes, status, checked_at) "
                        "VALUES (?, ?, ?, ?, 'ok', datetime('now'))",
                        (sha, ext, kind, size))
                elif ent["status"] != "ok":
                    # 重传同一文件即修复 missing/corrupt 实体
                    conn.execute(
                        "UPDATE media_entities SET ext=?, kind=?, size_bytes=?, "
                        "status='ok', checked_at=datetime('now') WHERE sha256=?",
                        (ext, kind, size, sha))
                cur = conn.execute(
                    "INSERT INTO media_items (entity_sha, name, original_name, "
                    "mime_type) VALUES (?, ?, ?, ?)",
                    (sha, name, original_name, mime))
                item_id = cur.lastrowid
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return get_item(item_id)


def list_items(search: str | None = None) -> dict:
    """素材条目列表（按名称 / 原始文件名模糊检索），含绑定数与就绪状态。"""
    sql = (
        "SELECT mi.id, mi.name, mi.original_name, mi.mime_type, mi.entity_sha, "
        "mi.created_at, me.kind, me.ext, me.size_bytes, me.status AS db_status, "
        "(SELECT COUNT(*) FROM cue_media cm WHERE cm.media_item_id=mi.id) "
        "  AS ref_count "
        "FROM media_items mi JOIN media_entities me ON me.sha256 = mi.entity_sha")
    args: list = []
    if search:
        sql += " WHERE mi.name LIKE ? OR mi.original_name LIKE ?"
        like = f"%{search.strip()}%"
        args = [like, like]
    sql += " ORDER BY mi.id DESC"
    with database.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        shas = {r["entity_sha"] for r in rows}
        statuses = quick_status_map(conn, shas) if shas else {}
        total_entities = conn.execute(
            "SELECT COUNT(*) AS c FROM media_entities").fetchone()["c"]
    items = [{
        "id": r["id"],
        "name": r["name"],
        "original_name": r["original_name"],
        "mime_type": r["mime_type"],
        "kind": r["kind"],
        "sha256": r["entity_sha"],
        "size_bytes": r["size_bytes"],
        "status": statuses.get(r["entity_sha"], r["db_status"]),
        "ready": statuses.get(r["entity_sha"], r["db_status"]) == "ok",
        "ref_count": r["ref_count"],
        "created_at": r["created_at"],
        "url": f"/api/media/{r['id']}/content",
    } for r in rows]
    return {"items": items, "count": len(items), "entity_count": total_entities}


def get_item(item_id: int) -> dict:
    with database.db() as conn:
        row = conn.execute(
            "SELECT mi.id, mi.name, mi.original_name, mi.mime_type, mi.entity_sha,"
            " mi.created_at, me.kind, me.ext, me.size_bytes, me.status "
            "FROM media_items mi JOIN media_entities me "
            "ON me.sha256 = mi.entity_sha WHERE mi.id=?",
            (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "素材条目不存在")
        r = dict(row)
        st = quick_status_map(conn, [r["entity_sha"]])[r["entity_sha"]]
        cues = [dict(x) for x in conn.execute(
            "SELECT c.id, c.name, c.department FROM cue_media cm "
            "JOIN cues c ON c.id = cm.cue_id WHERE cm.media_item_id=? "
            "ORDER BY c.id", (item_id,)).fetchall()]
    return {
        "id": r["id"],
        "name": r["name"],
        "original_name": r["original_name"],
        "mime_type": r["mime_type"],
        "kind": r["kind"],
        "sha256": r["entity_sha"],
        "ext": r["ext"],
        "size_bytes": r["size_bytes"],
        "status": st,
        "ready": st == "ok",
        "cues": cues,
        "url": f"/api/media/{r['id']}/content",
    }


def delete_item(item_id: int) -> dict:
    """删除素材条目；被提示引用时 409；最后一个同哈希条目才回收实体与文件。"""
    with database.db() as conn:
        row = conn.execute(
            "SELECT id, entity_sha FROM media_items WHERE id=?",
            (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "素材条目不存在")
        sha = row["entity_sha"]
        with _sha_lock(sha):
            try:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                refs = conn.execute(
                    "SELECT cue_id FROM cue_media WHERE media_item_id=?",
                    (item_id,)).fetchall()
                if refs:
                    shown = ", ".join(f"#{r['cue_id']}" for r in refs[:5])
                    more = "…" if len(refs) > 5 else ""
                    raise HTTPException(
                        409, f"素材条目被 {len(refs)} 条提示引用"
                             f"（{shown}{more}），请先解除绑定后再删除")
                ent = conn.execute(
                    "SELECT ext FROM media_entities WHERE sha256=?",
                    (sha,)).fetchone()
                conn.execute("DELETE FROM media_items WHERE id=?", (item_id,))
                remaining = conn.execute(
                    "SELECT COUNT(*) AS c FROM media_items WHERE entity_sha=?",
                    (sha,)).fetchone()["c"]
                reclaimed = False
                if remaining == 0 and ent is not None:
                    # 最后一个条目：先在事务内删除实体行（RESTRICT 兜底），
                    # 物理文件等提交成功后再回收。
                    conn.execute("DELETE FROM media_entities WHERE sha256=?",
                                 (sha,))
                    reclaimed = True
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # 事务提交成功后才回收物理文件；此时实体行已不存在，
        # 并发上传若重传同内容会走「实体不存在」分支重新落位，不会读到缺文件。
        path = entity_path(sha, ent["ext"]) if reclaimed else None
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
            # 清理可能留空的分桶目录（忽略失败）
            for d in (path.parent, path.parent.parent):
                try:
                    d.rmdir()
                except OSError:
                    break
    return {"id": item_id, "deleted": True, "entity_reclaimed": reclaimed}


# ---------------------------------------------------------------- 提示绑定

def set_cue_bindings(cue_id: int, item_ids: list[int]) -> dict:
    """全量替换某条提示绑定的素材；条目不存在 / 去重校验 -> 400。"""
    if len(item_ids) != len(set(item_ids)):
        raise HTTPException(400, "同一素材不能重复绑定")
    with database.db() as conn:
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM cues WHERE id=?",
                            (cue_id,)).fetchone() is None:
                raise HTTPException(404, "提示不存在")
            conn.execute("DELETE FROM cue_media WHERE cue_id=?", (cue_id,))
            for mid in item_ids:
                if conn.execute("SELECT 1 FROM media_items WHERE id=?",
                                (mid,)).fetchone() is None:
                    raise HTTPException(400, f"素材条目 #{mid} 不存在")
                conn.execute(
                    "INSERT INTO cue_media (cue_id, media_item_id) "
                    "VALUES (?, ?)", (cue_id, mid))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return _bindings(cue_id)


def _bindings(cue_id: int) -> dict:
    with database.db() as conn:
        return {"cue_id": cue_id, "media": attach_media_to_cues(conn, [cue_id])[cue_id]}


# ---------------------------------------------------------------- 完整性检查

def verify_all(deep: bool = True) -> dict:
    """全量完整性检查：实体缺失或哈希不符时标记，返回汇总与受影响提示。"""
    with database.db() as conn:
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            ents = [dict(r) for r in conn.execute(
                "SELECT sha256, ext, kind, size_bytes, status "
                "FROM media_entities ORDER BY sha256").fetchall()]
            results = []
            for ent in ents:
                if deep:
                    info = _check_entity(conn, ent)
                else:
                    path = entity_path(ent["sha256"], ent["ext"])
                    if not path.exists():
                        info = {"status": "missing", "reason": "实体文件缺失"}
                    elif path.stat().st_size != ent["size_bytes"]:
                        info = {"status": "corrupt", "reason": "文件大小不符"}
                    else:
                        info = {"status": "ok", "reason": ""}
                    _set_status(conn, ent["sha256"], info["status"])
                results.append({
                    "sha256": ent["sha256"],
                    "kind": ent["kind"],
                    "size_bytes": ent["size_bytes"],
                    **info,
                })
            bad = {r["sha256"] for r in results if r["status"] != "ok"}
            affected_rows = []
            if bad:
                marks = ",".join("?" for _ in bad)
                affected_rows = [dict(r) for r in conn.execute(
                    f"SELECT DISTINCT cm.cue_id, c.name, c.department, "
                    f"mi.entity_sha FROM cue_media cm "
                    f"JOIN cues c ON c.id = cm.cue_id "
                    f"JOIN media_items mi ON mi.id = cm.media_item_id "
                    f"WHERE mi.entity_sha IN ({marks})", list(bad)).fetchall()]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    affected = {}
    for r in affected_rows:
        affected.setdefault(r["cue_id"], {
            "cue_id": r["cue_id"], "name": r["name"],
            "department": r["department"], "shas": []})
        if r["entity_sha"] not in affected[r["cue_id"]]["shas"]:
            affected[r["cue_id"]]["shas"].append(r["entity_sha"])
    counts = {"ok": 0, "missing": 0, "corrupt": 0}
    for r in results:
        counts[r["status"]] += 1
    return {
        "checked": len(results),
        "counts": counts,
        "entities": results,
        "affected_cues": list(affected.values()),
        "all_ready": counts["missing"] == 0 and counts["corrupt"] == 0,
    }


# ---------------------------------------------------------------- 启动时对账

def cleanup_staging() -> int:
    """清理临时目录中所有未完成的 .part（崩溃 / 重启残骸）。返回清理数量。"""
    init_storage()
    n = 0
    for p in TMP_DIR.glob("*.part"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def reconcile_on_startup() -> None:
    """容器启动时：清理临时残骸，并按物理文件实际状态修正实体标记。

    数据库有记录但物理文件不存在 -> missing；大小不符 -> corrupt。
    不删除任何持久化文件（无法安全判断孤儿归属时保留，待同内容重传或人工处理）。
    """
    cleanup_staging()
    with database.db() as conn:
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            ents = [dict(r) for r in conn.execute(
                "SELECT sha256, ext, size_bytes FROM media_entities").fetchall()]
            for ent in ents:
                path = entity_path(ent["sha256"], ent["ext"])
                if not path.exists():
                    status = "missing"
                elif path.stat().st_size != ent["size_bytes"]:
                    status = "corrupt"
                else:
                    status = "ok"
                conn.execute(
                    "UPDATE media_entities SET status=?, "
                    "checked_at=datetime('now') WHERE sha256=?",
                    (status, ent["sha256"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
