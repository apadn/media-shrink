"""转码历史：记录每次成功的转码，持久化到数据卷。

两个用途：

1. **回看**：什么时候压了什么文件、从多大压到多大、用的什么预设和编码器。
2. **避免重复转码**：扫描时用「文件大小 + mtime」当指纹去比对历史，命中就标成
   「已转过」并且不默认勾选。文件被替换过（重压、重新下载）指纹就对不上，
   不会被误判。

还有一类坑单独处理：压缩完把输出改名回源文件名之后，那个文件既没有
`_compressed` 后缀、内容也变了，下一轮扫描会被当成全新文件再压一遍。
所以记录里同时存输出路径和输出指纹，扫描时反查一次就能认出来。

落盘是 JSON + 原子替换（先写临时文件再 os.replace），断电最多丢最后一次写入。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

from . import config

log = logging.getLogger("media-shrink.history")


def file_sig(path: str) -> str:
    """文件指纹：大小 : mtime(秒)。文件被替换或重压过必然对不上。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}:{int(st.st_mtime)}"


def _real(path: str) -> str:
    return os.path.realpath(path) if path else ""


class HistoryStore:
    def __init__(self, path: str = "", limit: int = 0) -> None:
        self.path = path or config.HISTORY_FILE
        self.limit = limit or config.HISTORY_MAX
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []   # 新→旧
        self._by_src: dict[str, dict[str, Any]] = {}
        self._by_out: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._warned = False

    # ------------------------------------------------------------ 读写
    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # 文件坏了不能让它拖垮启动：重命名留档，从空历史开始
            log.warning("转码历史读取失败（%s），将从空历史开始：%s", self.path, exc)
            try:
                os.replace(self.path, f"{self.path}.broken")
            except OSError:
                pass
            return
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        for rec in items:
            if isinstance(rec, dict) and rec.get("src") and rec.get("out"):
                self._items.append(rec)
        self._reindex()

    def _reindex(self) -> None:
        self._by_src.clear()
        self._by_out.clear()
        # 列表是「新→旧」，反着遍历让最新的记录赢
        for rec in reversed(self._items):
            src = _real(rec.get("src", ""))
            out = _real(rec.get("out", ""))
            if src:
                self._by_src[src] = rec
            if out:
                self._by_out[out] = rec

    def _save_locked(self) -> None:
        if not self.path:
            return
        tmp = ""
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".json", dir=d or None)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "items": self._items}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            tmp = ""
        except OSError as exc:
            if not self._warned:
                self._warned = True
                log.warning("转码历史写入失败（%s）：%s —— 仅本次运行内存里有效", self.path, exc)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------ 写入
    def record(
        self,
        *,
        job_id: str,
        src: str,
        out: str,
        size_before: int,
        size_after: int,
        duration: float = 0.0,
        preset_id: str = "",
        preset_label: str = "",
        encoder: str = "",
        encoder_kind: str = "",
        container: str = "",
        started_at: float = 0.0,
        finished_at: float = 0.0,
        source_action: str = "",
        renamed_to: str = "",
    ) -> dict[str, Any] | None:
        """记一条成功的转码。同一源文件只保留最新一条，避免历史被反复重压刷屏。"""
        if not src or not out:
            return None
        rec = {
            "id": job_id,
            "src": src,
            "out": renamed_to or out,
            "src_sig": file_sig(src),
            "out_sig": file_sig(renamed_to or out),
            "size_before": int(size_before or 0),
            "size_after": int(size_after or 0),
            "duration": round(float(duration or 0), 2),
            "preset_id": preset_id,
            "preset_label": preset_label,
            "encoder": encoder,
            "encoder_kind": encoder_kind,
            "container": container,
            "started_at": started_at,
            "finished_at": finished_at or time.time(),
            "source_action": source_action,
        }
        with self._lock:
            self._ensure()
            src_key = _real(src)
            self._items = [r for r in self._items if _real(r.get("src", "")) != src_key]
            self._items.insert(0, rec)
            if len(self._items) > self.limit:
                del self._items[self.limit:]
            self._reindex()
            self._save_locked()
        return rec

    def update_out(self, src: str, new_out: str) -> None:
        """输出被改名（比如改回源文件名）后，同步历史里的路径与指纹。

        不改这条记录的话，下一轮扫描会把这个文件当成新文件——它既没有了
        `_compressed` 后缀，内容也和当初的源文件不同，指纹反查是唯一线索。
        """
        key = _real(src)
        with self._lock:
            self._ensure()
            rec = self._by_src.get(key)
            if not rec:
                return
            rec["out"] = new_out
            rec["out_sig"] = file_sig(new_out)
            self._reindex()
            self._save_locked()

    def mark_source_action(self, src: str, action: str) -> None:
        key = _real(src)
        with self._lock:
            self._ensure()
            rec = self._by_src.get(key)
            if not rec:
                return
            rec["source_action"] = action
            self._save_locked()

    # ------------------------------------------------------------ 查询
    def check(self, path: str) -> dict[str, Any] | None:
        """这个文件是不是已经转过了。

        返回历史记录 + `match` 字段：
        - `src`：它就是当初的源文件（指纹一致），别重复压
        - `out`：它是当初的转码产物（指纹一致），更别压
        返回 None 表示没转过，或文件已经被替换过（指纹对不上）。
        """
        if not path:
            return None
        cur = file_sig(path)
        if not cur:
            return None
        key = _real(path)
        with self._lock:
            self._ensure()
            rec = self._by_src.get(key)
            if rec and rec.get("src_sig") == cur:
                return {**rec, "match": "src"}
            rec = self._by_out.get(key)
            if rec and rec.get("out_sig") == cur:
                return {**rec, "match": "out"}
        return None

    def listing(self, query: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        q = (query or "").strip().lower()
        with self._lock:
            self._ensure()
            items = [r for r in self._items
                     if not q or q in str(r.get("src", "")).lower() or q in str(r.get("out", "")).lower()]
            total = len(items)
            page = items[max(0, offset):max(0, offset) + max(1, limit)]
            return {
                "total": total,
                "items": [dict(r) for r in page],
                "stats": self._stats_locked(items),
            }

    def _stats_locked(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        saved = 0
        before = 0
        after = 0
        for r in items:
            b = int(r.get("size_before") or 0)
            a = int(r.get("size_after") or 0)
            if b > 0 and a > 0:
                before += b
                after += a
                saved += max(0, b - a)
        return {
            "count": len(items),
            "bytes_before": before,
            "bytes_after": after,
            "bytes_saved": saved,
            "ratio": round(after / before, 3) if before else None,
        }

    def count(self) -> int:
        with self._lock:
            self._ensure()
            return len(self._items)

    def clear(self) -> int:
        with self._lock:
            self._ensure()
            n = len(self._items)
            self._items = []
            self._reindex()
            self._save_locked()
            return n


history = HistoryStore()
