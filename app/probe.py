"""ffprobe 封装：把媒体信息解析成扁平的 dict，供扫描与转码复用。"""
from __future__ import annotations

import json
import os
import subprocess
from fractions import Fraction
from typing import Any

from . import config

VIDEO_EXT = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".mts", ".webm",
    ".flv", ".wmv", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".ogv", ".vob", ".divx",
}

SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub", ".idx", ".smi"}

# 输出 mp4 时可直接 copy 的音频编码（opus 虽能封装但兼容性差，不放行）
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mp2"}
# 输出 webm 时可直接 copy 的音频编码
WEBM_AUDIO_OK = {"opus", "vorbis"}

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


class ProbeError(RuntimeError):
    pass


def _fraction_to_float(value: str | None, default: float = 0.0) -> float:
    if not value:
        return default
    try:
        if "/" in value:
            return float(Fraction(value))
        return float(value)
    except (ValueError, ZeroDivisionError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def probe(path: str) -> dict:
    """运行 ffprobe 并返回结构化信息，失败抛 ProbeError。"""
    cmd = [
        config.FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", path,
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as exc:
        raise ProbeError("找不到 ffprobe，请检查镜像或 FFPROBE_BIN") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe 超时") from exc

    if cp.returncode != 0:
        raise ProbeError((cp.stderr or "ffprobe 执行失败").strip().splitlines()[-1][:300])

    try:
        raw = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe 输出无法解析") from exc

    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []

    size = _int(fmt.get("size"), os.path.getsize(path) if os.path.exists(path) else 0)
    duration = _float(fmt.get("duration"))
    if duration <= 0:
        for s in streams:
            duration = max(duration, _float(s.get("duration")))
    fmt_bitrate = _int(fmt.get("bit_rate"))

    video_streams: list[dict] = []
    audio_streams: list[dict] = []
    sub_streams: list[dict] = []

    for s in streams:
        ctype = s.get("codec_type")
        disposition = s.get("disposition") or {}
        if ctype == "video":
            # 跳过内嵌封面图
            if disposition.get("attached_pic"):
                continue
            width = _int(s.get("width"))
            height = _int(s.get("height"))
            fps = _fraction_to_float(s.get("avg_frame_rate")) or _fraction_to_float(s.get("r_frame_rate"), 25.0)
            if fps <= 0 or fps > 240:
                fps = 25.0
            bitrate = _int(s.get("bit_rate"))
            transfer = (s.get("color_transfer") or "").lower()
            pix_fmt = s.get("pix_fmt") or ""
            video_streams.append({
                "index": s.get("index"),
                "codec": s.get("codec_name") or "unknown",
                "profile": s.get("profile") or "",
                "width": width,
                "height": height,
                "fps": round(fps, 3),
                "bitrate": bitrate,
                "pix_fmt": pix_fmt,
                "bit_depth": 10 if ("10" in pix_fmt or "p010" in pix_fmt) else 8,
                "hdr": transfer in HDR_TRANSFERS,
                "color_transfer": transfer,
                "color_primaries": (s.get("color_primaries") or "").lower(),
                "color_space": (s.get("color_space") or "").lower(),
                "duration": _float(s.get("duration"), duration),
            })
        elif ctype == "audio":
            audio_streams.append({
                "index": s.get("index"),
                "codec": s.get("codec_name") or "unknown",
                "channels": _int(s.get("channels"), 2),
                "channel_layout": s.get("channel_layout") or "",
                "bitrate": _int(s.get("bit_rate")),
                "language": (s.get("tags") or {}).get("language", ""),
                "title": (s.get("tags") or {}).get("title", ""),
                "default": bool(disposition.get("default")),
            })
        elif ctype == "subtitle":
            sub_streams.append({
                "index": s.get("index"),
                "codec": s.get("codec_name") or "unknown",
                "language": (s.get("tags") or {}).get("language", ""),
                "title": (s.get("tags") or {}).get("title", ""),
                "default": bool(disposition.get("default")),
                "forced": bool(disposition.get("forced")),
            })

    if not video_streams:
        raise ProbeError("文件中没有视频流")

    main = max(video_streams, key=lambda v: v["width"] * v["height"])

    # 无视频码率时，用总码率减去音频估算
    video_bitrate = main["bitrate"]
    if video_bitrate <= 0:
        audio_total = sum(a["bitrate"] for a in audio_streams)
        video_bitrate = max(0, fmt_bitrate - audio_total) if fmt_bitrate else 0
        main["bitrate"] = video_bitrate

    audio_bitrate = sum(a["bitrate"] for a in audio_streams)
    if audio_bitrate <= 0 and audio_streams:
        audio_bitrate = 128_000 * len(audio_streams)

    container = (fmt.get("format_name") or "").split(",")[0]
    duration = duration or main["duration"] or 0.0

    return {
        "path": path,
        "filename": os.path.basename(path),
        "size": size,
        "duration": round(duration, 3),
        "container": container,
        "format_long": fmt.get("format_long_name") or "",
        "total_bitrate": fmt_bitrate or (video_bitrate + audio_bitrate),
        "video": main,
        "video_streams": video_streams,
        "audio": audio_streams,
        "audio_bitrate": audio_bitrate,
        "subtitles": sub_streams,
        "chapters": len(raw.get("chapters") or []),
        "hdr": main["hdr"],
        "bit_depth": main["bit_depth"],
    }


def subtitle_codec_for_container(container: str, src_codec: str) -> tuple[str, str | None]:
    """返回 (输出字幕参数值, 说明)。container 为 mkv/mp4/webm。"""
    codec = (src_codec or "").lower()
    if container == "mp4":
        if codec in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt"):
            return "mov_text", "转 mov_text（mp4 只支持文本字幕）"
        return "mov_text", "转 mov_text（图形字幕无法存入 mp4）"
    if container == "webm":
        return "webvtt", "转 webvtt"
    # mkv 全兼容
    return "copy", None
