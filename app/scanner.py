"""目录扫描：找视频、关联外挂字幕、估算压缩收益、给出建议。"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config, presets, probe as probe_mod

SUB_DIR_HINTS = {"subs", "sub", "subtitles", "subtitle", "字幕"}

# 目标编码：已经是这两种的文件单独成表，默认不再压缩
TARGET_CODECS = {"av1": "AV1", "hevc": "HEVC"}


def codec_family(codec: str | None) -> str:
    """把 ffprobe 的 codec_name 归到 av1 / hevc / other。"""
    c = (codec or "").lower()
    if c in ("av1", "av01"):
        return "av1"
    if c in ("hevc", "h265"):
        return "hevc"
    return "other"

_cache: dict[str, tuple[float, int, dict]] = {}
_cache_lock = threading.Lock()


def media_id(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", "ignore")).hexdigest()[:16]


def is_allowed(path: str) -> bool:
    if not config.MEDIA_ROOTS:
        return True
    rp = os.path.realpath(path)
    for root in config.MEDIA_ROOTS:
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def probe_cached(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        raise probe_mod.ProbeError("文件不存在或无法访问")
    with _cache_lock:
        hit = _cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    info = probe_mod.probe(path)
    with _cache_lock:
        _cache[path] = (st.st_mtime, st.st_size, info)
    return info


# ---------------------------------------------------------------- 目录排除
def merge_patterns(extra: list[str] | None = None) -> list[str]:
    """配置里的默认排除项 + 本次扫描临时加上的（去重，保持顺序）。"""
    pats = list(config.EXCLUDE_DIRS)
    for raw in extra or []:
        p = str(raw).strip().strip("/")
        if p and p not in pats:
            pats.append(p)
    return pats


def is_excluded(
    rel_path: str, name: str, patterns: list[str] | None = None
) -> bool:
    """这个目录要不要跳过。

    patterns 里每项都是 glob（`*` / `?`），两条匹配规则：
      1. 只对目录名匹配 —— `@eaDir`、`*sample*`、`花絮`
      2. 含 `/` 时对「相对扫描根的路径」匹配 —— `movies/花絮`、`*/Extras`
    全部不区分大小写；空项忽略。
    """
    pats = config.EXCLUDE_DIRS if patterns is None else patterns
    low_name = name.lower()
    rel = rel_path.replace(os.sep, "/").strip("/").lower()
    for raw in pats:
        pat = str(raw).strip().strip("/").lower()
        if not pat:
            continue
        if fnmatch.fnmatchcase(low_name, pat):
            return True
        if "/" in pat and rel and fnmatch.fnmatchcase(rel, pat):
            return True
    return False


# ---------------------------------------------------------------- 文件遍历
def _walk(
    root: str, recursive: bool, patterns: list[str] | None = None
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    videos: list[str] = []
    subs: dict[str, list[str]] = {}
    skipped: list[str] = []
    root = os.path.abspath(root)
    max_depth = config.MAX_DEPTH

    if recursive:
        for cur, dirs, files in os.walk(root):
            rel = os.path.relpath(cur, root)
            keep = []
            for d in dirs:
                # 隐藏目录一律不碰（.git / .Trashes / @eaDir 的兄弟目录等）
                if d.startswith("."):
                    continue
                child = d if rel == "." else os.path.join(rel, d)
                if is_excluded(child, d, patterns):
                    skipped.append(os.path.join(cur, d))
                    continue
                keep.append(d)
            dirs[:] = keep
            depth = cur[len(root):].count(os.sep)
            if depth >= max_depth:
                dirs[:] = []
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in probe_mod.VIDEO_EXT:
                    videos.append(os.path.join(cur, f))
                elif ext in probe_mod.SUB_EXT:
                    subs.setdefault(cur, []).append(os.path.join(cur, f))
    else:
        with os.scandir(root) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in probe_mod.VIDEO_EXT:
                    videos.append(entry.path)
                elif ext in probe_mod.SUB_EXT:
                    subs.setdefault(root, []).append(entry.path)
    return videos, subs, skipped


def _match_subtitles(video_path: str, sub_index: dict[str, list[str]]) -> list[dict[str, Any]]:
    """同目录 + 常见字幕子目录里，文件名以视频名开头的字幕。"""
    vdir = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    candidates: list[str] = list(sub_index.get(vdir, []))

    # 字幕子目录（Subs / Subtitles / 字幕 ...）
    try:
        with os.scandir(vdir) as it:
            for entry in it:
                if entry.is_dir() and entry.name.lower() in SUB_DIR_HINTS:
                    with os.scandir(entry.path) as inner:
                        for f in inner:
                            if f.is_file() and os.path.splitext(f.name)[1].lower() in probe_mod.SUB_EXT:
                                candidates.append(f.path)
    except OSError:
        pass

    matched: list[dict[str, Any]] = []
    for path in candidates:
        name = os.path.basename(path)
        s = os.path.splitext(name)[0]
        if s == stem or s.startswith(stem + ".") or s.startswith(stem + "_"):
            suffix = s[len(stem):].lstrip("._-").replace("_", " ").strip()
            matched.append({
                "path": path,
                "filename": name,
                "ext": os.path.splitext(name)[1].lower().lstrip("."),
                "label": suffix or "默认轨",
                "size": os.path.getsize(path) if os.path.exists(path) else 0,
            })
    matched.sort(key=lambda x: x["filename"])
    return matched


# ---------------------------------------------------------------- 建议
def _suggest(media: dict, est_bytes: int, codec: str) -> tuple[str, str]:
    video = media["video"]
    size = media["size"] or 1
    ratio = est_bytes / size
    px = (video["width"] or 0) * (video["height"] or 0) * (video.get("fps") or 25)
    src_bpp = (video.get("bitrate") or 0) / px if px else 0
    src_codec = (video.get("codec") or "").lower()
    family = codec_family(src_codec)
    kbps = (video.get("bitrate") or 0) // 1000

    if family == "av1" and src_bpp < 0.028:
        return "skip", f"已是 AV1 且码率很低（{kbps} kbps），再压基本没有收益"
    if family == "hevc" and src_bpp < 0.032:
        return "skip", f"已是 HEVC 且码率很低（{kbps} kbps），压缩收益有限"
    if ratio >= 1.0:
        tag = "" if family == "other" else f"已是 {TARGET_CODECS[family]}，"
        return "skip", f"{tag}源码率已经很低（{kbps} kbps），重编码后体积可能不降反升"
    if ratio >= 0.92:
        if family != "other":
            return "skip", f"已是 {TARGET_CODECS[family]}，预估体积几乎不降，不值得重编码"
        return "skip", "预估体积几乎不降，不值得重编码"
    if ratio >= 0.78:
        tag = f"已是 {TARGET_CODECS[family]}，" if family != "other" else ""
        return "maybe", f"{tag}预计仅省 {int((1 - ratio) * 100)}%，收益一般"
    tag = f"已是 {TARGET_CODECS[family]}（{kbps} kbps 码率偏高），" if family != "other" else ""
    return "yes", f"{tag}预计省 {int((1 - ratio) * 100)}%，值得压缩"


def _own_artifact(path: str) -> str:
    """识别本工具自己产生的文件，返回跳过原因；不是则返回空串。

    - `xxx_compressed.mkv`：本工具的输出，不能再压一次
    - `xxx.old.mkv`：压缩后改名归档的源文件，等用户确认后再清理
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    suffix = (config.OUTPUT_SUFFIX or "").strip()
    # 同名文件压第二次会生成 xxx_compressed_1，也要认出来
    if suffix and re.search(rf"{re.escape(suffix)}(_\d+)?$", stem):
        return "本工具生成的输出文件"
    old = (config.SOURCE_OLD_SUFFIX or "").strip().strip(".")
    if old and re.search(rf"\.{re.escape(old)}\d*$", stem, re.IGNORECASE):
        return "压缩前改名归档的源文件（.old），确认无误后可手动清理"
    return ""


def _build_item(path: str, media: dict, preset: dict, preference: str, custom_crf: float | None) -> dict[str, Any]:
    codec = preset.get("codec") or media["video"]["codec"]
    enc = None
    if preset.get("codec"):
        enc = presets_encoder(codec, preference)
    est = presets.estimate_size(media, preset, codec if preset.get("codec") else None, custom_crf)
    suggestion, reason = _suggest(media, est["bytes"], codec)
    if preset.get("codec") is None:
        suggestion, reason = "maybe", "仅重封装，画质无损但体积变化很小"
    artifact = _own_artifact(path)
    if artifact:
        suggestion, reason = "skip", artifact

    family = codec_family(media["video"]["codec"])

    return {
        "id": media_id(path),
        "path": path,
        "filename": media["filename"],
        "dir": os.path.dirname(path),
        "size": media["size"],
        "duration": media["duration"],
        "container": media["container"],
        "width": media["video"]["width"],
        "height": media["video"]["height"],
        "fps": media["video"]["fps"],
        "codec": media["video"]["codec"],
        "codec_family": family,
        "target_codec": family in TARGET_CODECS,
        "bitrate": media["video"]["bitrate"],
        "pix_fmt": media["video"]["pix_fmt"],
        "hdr": media["hdr"],
        "bit_depth": media["bit_depth"],
        "audio": [
            {"index": a["index"], "codec": a["codec"], "channels": a["channels"],
             "channel_layout": a.get("channel_layout", ""), "language": a["language"],
             "title": a["title"], "bitrate": a["bitrate"], "default": a.get("default", False)}
            for a in media["audio"]
        ],
        "embedded_subtitles": media["subtitles"],
        "chapters": media["chapters"],
        "estimate_bytes": est["bytes"],
        "estimate_ratio": est["ratio"],
        "suggestion": suggestion,
        "reason": reason,
        "encoder": (enc or {}).get("name"),
        "encoder_kind": (enc or {}).get("kind"),
    }


def presets_encoder(codec: str, preference: str) -> dict[str, Any] | None:
    from .hwdetect import probe as hw
    return hw.pick(codec, preference)


# ---------------------------------------------------------------- 主入口
def list_dir(path: str) -> dict[str, Any]:
    if not is_allowed(path):
        raise PermissionError("路径不在允许的媒体根目录内")
    if not os.path.isdir(path):
        raise FileNotFoundError("目录不存在")
    dirs, files = [], []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith(".") or is_excluded(e.name, e.name):
                continue
            if e.is_dir():
                dirs.append({"name": e.name, "path": e.path})
            elif os.path.splitext(e.name)[1].lower() in probe_mod.VIDEO_EXT:
                files.append({"name": e.name, "path": e.path,
                              "size": e.stat().st_size if e.is_file() else 0})
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(path.rstrip(os.sep)) or "/"
    if not is_allowed(parent):
        parent = None
    return {"path": path, "parent": parent, "dirs": dirs, "files": files,
            "roots": config.MEDIA_ROOTS}


def scan(
    root: str,
    recursive: bool = True,
    preset_id: str = presets.DEFAULT_PRESET_ID,
    preference: str = "auto",
    custom_crf: float | None = None,
    min_size_mb: float | None = None,
    manual: dict[str, Any] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    if not is_allowed(root):
        raise PermissionError("路径不在允许的媒体根目录内")
    if not os.path.isdir(root):
        raise FileNotFoundError("目录不存在")

    preset = presets.get_preset(preset_id, manual)
    threshold = (config.MIN_SIZE_MB if min_size_mb is None else min_size_mb) * 1024 * 1024
    patterns = merge_patterns(exclude)

    videos, sub_index, skipped = _walk(root, recursive, patterns)
    videos = [v for v in videos if _safe_size(v) >= threshold]

    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def work(path: str):
        try:
            media = probe_cached(path)
        except probe_mod.ProbeError as exc:
            return ("err", path, str(exc))
        except Exception as exc:  # noqa: BLE001
            return ("err", path, f"读取失败：{exc}")
        item = _build_item(path, media, preset, preference, custom_crf)
        item["subtitles"] = _match_subtitles(path, sub_index)
        return ("ok", item, None)

    with ThreadPoolExecutor(max_workers=min(6, max(2, (os.cpu_count() or 4)))) as pool:
        for kind, a, b in pool.map(work, videos):
            if kind == "ok":
                items.append(a)
            else:
                errors.append({"path": a, "error": b})

    # 先按「是否已是目标编码」分两大组，组内再按建议与体量排序
    convert = [i for i in items if i["codec_family"] == "other"]
    already = [i for i in items if i["codec_family"] != "other"]
    convert.sort(key=lambda x: (x["suggestion"] != "yes", -x["estimate_bytes"]))
    already.sort(key=lambda x: (x["suggestion"] != "yes", -x["size"]))
    items = convert + already

    total_size = sum(i["size"] for i in items)
    total_est = sum(i["estimate_bytes"] for i in items)
    return {
        "root": root,
        "preset": preset_id,
        "preset_label": preset.get("label"),
        "manual": presets.normalize_manual(manual) if preset.get("manual") else None,
        "exclude": patterns,
        "items": items,
        "errors": errors,
        "stats": {
            "count": len(items),
            "convert_count": len(convert),
            "already_count": len(already),
            "already_av1": sum(1 for i in already if i["codec_family"] == "av1"),
            "already_hevc": sum(1 for i in already if i["codec_family"] == "hevc"),
            "already_recompress_count": sum(1 for i in already if i["suggestion"] == "yes"),
            "total_size": total_size,
            "estimate_size": total_est,
            "recommend_count": sum(1 for i in items if i["suggestion"] == "yes"),
            "external_subtitle_count": sum(1 for i in items if i["subtitles"]),
            "embedded_subtitle_count": sum(1 for i in items if i["embedded_subtitles"]),
            "excluded_dirs": len(skipped),
        },
    }


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def reestimate(items: list[dict[str, Any]], preference: str) -> dict[str, Any]:
    """切换预设 / 改手动参数 / 改音轨后重算预估（复用 probe 缓存，很快）。

    items: [{path, preset_id?, manual?, custom_crf?, tracks?}]
    tracks 为音轨流 index 列表；None 表示保留全部音轨。
    每个文件可以带各自的手动配置，所以逐条计算而不是按预设分组。
    """
    out: dict[str, Any] = {}
    for item in items:
        path = item.get("path")
        if not path:
            continue
        try:
            media = probe_cached(path)
        except Exception:  # noqa: BLE001
            continue
        preset = presets.get_preset(item.get("preset_id") or presets.DEFAULT_PRESET_ID, item.get("manual"))
        codec = preset.get("codec") or media["video"]["codec"]
        enc = presets_encoder(codec, preference) if preset.get("codec") else None
        est = presets.estimate_size(
            media, preset, codec if preset.get("codec") else None,
            item.get("custom_crf"), audio_tracks=item.get("tracks"),
        )
        suggestion, reason = _suggest(media, est["bytes"], codec)
        if preset.get("codec") is None:
            suggestion, reason = "maybe", "仅重封装，画质无损但体积变化很小"
        artifact = _own_artifact(path)
        if artifact:
            suggestion, reason = "skip", artifact
        out[path] = {
            "estimate_bytes": est["bytes"],
            "estimate_ratio": est["ratio"],
            "suggestion": suggestion,
            "reason": reason,
            "encoder": (enc or {}).get("name"),
            "encoder_kind": (enc or {}).get("kind"),
            "preset_label": preset.get("label"),
        }
    return out
