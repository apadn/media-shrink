"""预设矩阵与编码参数映射。

设计原则：
- 预设只描述"目标"（编码家族 + 质量档 + 音频策略），
- 具体参数在运行时依据实际可用的编码器（CPU / QSV / NVENC / VAAPI）映射，
- 所以同一套预设在任何机器上都能用，只是背后走的编码器不同。
"""
from __future__ import annotations

import math
from typing import Any

from . import config

# ---------------------------------------------------------------- 质量档 -> CRF
# AV1 与 HEVC/H.264 的 CRF 刻度不可直接比较，分别维护
CRF_TABLE: dict[str, dict[str, float]] = {
    "av1": {"max": 23, "high": 27, "balanced": 31, "small": 34, "insane": 37},
    "hevc": {"max": 18, "high": 21, "balanced": 25, "small": 28, "insane": 31},
    "h264": {"max": 18, "high": 20, "balanced": 23, "small": 26, "insane": 29},
}

# 硬件编码器相比软件在同等数值下画质略低，偏移补偿
HW_QP_OFFSET = 2

# SVT-AV1 preset（0 最慢最好）
SVT_PRESET = {"max": 4, "high": 5, "balanced": 6, "small": 8, "insane": 10}
X265_PRESET = {"max": "slow", "high": "medium", "balanced": "medium", "small": "fast", "insane": "faster"}
X264_PRESET = {"max": "slow", "high": "medium", "balanced": "medium", "small": "fast", "insane": "veryfast"}
QSV_PRESET = {"max": "veryslow", "high": "slower", "balanced": "medium", "small": "fast", "insane": "veryfast"}
NVENC_PRESET = {"max": "p7", "high": "p6", "balanced": "p5", "small": "p4", "insane": "p3"}

# 每像素每帧比特数（用于体积预估）
BPP = {
    "av1": {"max": 0.062, "high": 0.045, "balanced": 0.032, "small": 0.022, "insane": 0.015},
    "hevc": {"max": 0.075, "high": 0.056, "balanced": 0.042, "small": 0.030, "insane": 0.021},
    "h264": {"max": 0.100, "high": 0.078, "balanced": 0.060, "small": 0.044, "insane": 0.032},
}

AUDIO_BITRATE_BY_CH = {1: 96, 2: 128, 3: 192, 4: 256, 6: 320, 8: 512}

# 同画质下新编码器相对源编码器所需的码率比例（用于体积预估的源码率锚定）。
# 依据：HEVC 相对 H.264 同画质约省 30%，AV1 相对 H.264 约省 40%；同编码器重编码只省个位数。
CODEC_EFFICIENCY = {
    ("h264", "hevc"): 0.72,
    ("h264", "av1"): 0.58,
    ("h264", "h264"): 0.95,
    ("hevc", "av1"): 0.82,
    ("hevc", "hevc"): 0.92,
    ("av1", "av1"): 0.92,
    ("av1", "hevc"): 1.05,
}
# MPEG-2 / VC-1 / WMV / MPEG-4 这类老编码效率差得远，压起来收益最大
CODEC_EFFICIENCY_LEGACY = 0.55

_CODEC_ALIASES = {"h265": "hevc", "avc": "h264", "avc1": "h264", "hevc": "hevc",
                  "h264": "h264", "av1": "av1"}


def codec_efficiency(src_codec: str | None, dst_codec: str) -> float:
    """源编码 -> 目标编码，同画质所需码率的比例。"""
    src = _CODEC_ALIASES.get((src_codec or "").lower(), (src_codec or "").lower())
    if (src, dst_codec) in CODEC_EFFICIENCY:
        return CODEC_EFFICIENCY[(src, dst_codec)]
    if src in ("h264", "hevc", "av1"):
        return 0.8
    return CODEC_EFFICIENCY_LEGACY

# 画质优先是默认取向：高画质档 + 硬编码普遍支持，避免默认落到很慢的 AV1 软件编码
DEFAULT_PRESET_ID = "hevc-high"

MANUAL_PRESET_ID = "custom"

# ---------------------------------------------------------------- 手动配置
# 预设负责"常用组合一键到位"，手动配置负责"剩下 10% 的特殊需求"。
# 两者共用同一套参数构造逻辑：手动配置先被归一化成一个和预设同构的 dict。
MANUAL_CODECS = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264"}
MANUAL_MODES = ("crf", "bitrate")
MANUAL_SPEEDS = ("ultrafast", "fast", "medium", "slow", "veryslow")
MANUAL_DEPTHS = ("auto", "8", "10")
MANUAL_AUDIO = ("copy", "aac", "opus")

MANUAL_DEFAULTS: dict[str, Any] = {
    "codec": "hevc",
    "mode": "crf",          # crf = 恒定质量；bitrate = 目标码率
    "crf": 21,
    "bitrate": 4000,        # kbps，仅视频
    "speed": "medium",
    "max_height": 0,        # 0 = 保持原分辨率
    "bit_depth": "auto",    # auto | 8 | 10
    "audio": "copy",
    "audio_bitrate": 0,     # 0 = 按声道数自动
}

# 统一五档速度 -> 各编码器的实际参数
SPEED_TABLE: dict[str, dict[str, Any]] = {
    "x265": {"ultrafast": "ultrafast", "fast": "fast", "medium": "medium",
             "slow": "slow", "veryslow": "veryslow"},
    "x264": {"ultrafast": "ultrafast", "fast": "fast", "medium": "medium",
             "slow": "slow", "veryslow": "veryslow"},
    "svtav1": {"ultrafast": 12, "fast": 9, "medium": 6, "slow": 4, "veryslow": 2},
    "qsv": {"ultrafast": "veryfast", "fast": "fast", "medium": "medium",
            "slow": "slow", "veryslow": "veryslow"},
    "nvenc": {"ultrafast": "p2", "fast": "p4", "medium": "p5", "slow": "p6", "veryslow": "p7"},
    "amf": {"ultrafast": "speed", "fast": "speed", "medium": "balanced",
            "slow": "quality", "veryslow": "quality"},
}


def _clamp_num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return float(default)
    if n != n:  # NaN
        return float(default)
    return max(lo, min(hi, n))


def normalize_manual(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把前端传来的任意值收敛成合法配置，绝不因脏数据让任务失败。"""
    raw = raw if isinstance(raw, dict) else {}
    codec = str(raw.get("codec") or MANUAL_DEFAULTS["codec"]).lower()
    if codec not in MANUAL_CODECS:
        codec = MANUAL_DEFAULTS["codec"]
    mode = str(raw.get("mode") or MANUAL_DEFAULTS["mode"]).lower()
    if mode not in MANUAL_MODES:
        mode = MANUAL_DEFAULTS["mode"]
    speed = str(raw.get("speed") or MANUAL_DEFAULTS["speed"]).lower()
    if speed not in MANUAL_SPEEDS:
        speed = MANUAL_DEFAULTS["speed"]
    depth = str(raw.get("bit_depth") or "auto").lower()
    if depth not in MANUAL_DEPTHS:
        depth = "auto"
    audio = str(raw.get("audio") or "copy").lower()
    if audio not in MANUAL_AUDIO:
        audio = "copy"

    height = int(_clamp_num(raw.get("max_height"), 0, 0, 4320))
    return {
        "codec": codec,
        "mode": mode,
        "crf": _clamp_num(raw.get("crf"), MANUAL_DEFAULTS["crf"], 0, 63),
        "bitrate": int(_clamp_num(raw.get("bitrate"), MANUAL_DEFAULTS["bitrate"], 200, 200_000)),
        "speed": speed,
        "max_height": height if height >= 240 else 0,
        "bit_depth": depth,
        "audio": audio,
        "audio_bitrate": int(_clamp_num(raw.get("audio_bitrate"), 0, 0, 1024)),
    }


def manual_label(m: dict[str, Any]) -> str:
    codec = MANUAL_CODECS.get(m["codec"], m["codec"])
    if m["mode"] == "bitrate":
        rate = f"{m['bitrate']}kbps"
    else:
        rate = f"CRF {m['crf']:g}"
    return f"手动 · {codec} {rate}"


def manual_preset(m: dict[str, Any]) -> dict[str, Any]:
    """把手动配置投影成与预设同构的结构，后续走完全相同的代码路径。"""
    return {
        "id": MANUAL_PRESET_ID,
        "label": manual_label(m),
        "codec": m["codec"],
        "quality": "custom",
        "audio": m["audio"],
        "badge": "手动",
        "desc": "自己指定编码、质量/码率、速度、分辨率、位深与音频参数。",
        "manual": True,
        "crf": float(m["crf"]),
        "bitrate_kbps": int(m["bitrate"]) if m["mode"] == "bitrate" else None,
        "speed": m["speed"],
        "max_height": m["max_height"] or None,
        "bit_depth": m["bit_depth"],
        "audio_kbps": m["audio_bitrate"] or None,
    }


# ---------------------------------------------------------------- 预设定义
PRESETS: list[dict[str, Any]] = [
    # ---------------- AV1 ----------------
    {
        "id": "av1-balanced", "label": "AV1 · 均衡", "codec": "av1", "quality": "balanced",
        "audio": "copy", "badge": "均衡",
        "desc": "AV1 在同等画质下比 HEVC 再省 20~30%，适合长期归档。编码较慢，硬编码可加速。",
    },
    {
        "id": "av1-small", "label": "AV1 · 极致压缩", "codec": "av1", "quality": "small",
        "audio": "opus", "badge": "体积最小",
        "desc": "体积优先，画质有可见损失。适合空间极度紧张、主要在手机/平板观看的片库。",
    },
    {
        "id": "av1-high", "label": "AV1 · 高画质", "codec": "av1", "quality": "high",
        "audio": "copy", "badge": "画质优先",
        "desc": "接近视觉无损，体积仍显著小于原片。适合收藏级片源。",
    },
    {
        "id": "av1-max", "label": "AV1 · 视觉无损", "codec": "av1", "quality": "max",
        "audio": "copy", "badge": "",
        "desc": "几乎看不出差异，压缩率最低。仅在原片码率虚高时才有意义。",
    },
    {
        "id": "av1-anime", "label": "AV1 · 动画/番剧", "codec": "av1", "quality": "small",
        "audio": "copy", "badge": "动画",
        "desc": "关闭胶片颗粒合成，动画素材可再省 15~25% 体积而不糊。",
        "tune": "anime",
    },
    {
        "id": "av1-4k", "label": "AV1 · 4K/HDR 保留", "codec": "av1", "quality": "high",
        "audio": "copy", "badge": "4K",
        "desc": "保留 10bit 与 HDR 元数据，4K 片源专用。没有 AV1 硬解的设备慎用。",
    },
    # ---------------- HEVC ----------------
    {
        "id": "hevc-balanced", "label": "HEVC · 均衡", "codec": "hevc", "quality": "balanced",
        "audio": "copy", "badge": "均衡",
        "desc": "兼容性与压缩率的最佳平衡，手机/电视/播放器几乎都支持硬解。",
    },
    {
        "id": "hevc-small", "label": "HEVC · 极致压缩", "codec": "hevc", "quality": "small",
        "audio": "aac", "badge": "体积最小",
        "desc": "画质优先让位于体积，音轨同时转 AAC 省空间。",
    },
    {
        "id": "hevc-high", "label": "HEVC · 高画质（推荐）", "codec": "hevc", "quality": "high",
        "audio": "copy", "badge": "画质优先",
        "desc": "大屏观看友好，压缩率适中。画质优先时的默认选择，硬编码支持最广。",
    },
    {
        "id": "hevc-hdr", "label": "HEVC 10bit · HDR 原样保留", "codec": "hevc", "quality": "high",
        "audio": "copy", "badge": "HDR",
        "desc": "强制 10bit 输出，完整保留 HDR10/HLG 元数据与色彩标记。",
        "force_10bit": True,
    },
    {
        "id": "hevc-anime", "label": "HEVC · 动画/番剧", "codec": "hevc", "quality": "small",
        "audio": "copy", "badge": "动画",
        "desc": "针对平涂色块与线条优化，减少动画片源的码率浪费。",
        "tune": "anime",
    },
    # ---------------- H.264 ----------------
    {
        "id": "h264-compat", "label": "H.264 · 兼容优先", "codec": "h264", "quality": "balanced",
        "audio": "aac", "badge": "兼容",
        "desc": "老设备/老播放器/网页播放的保险选择，压缩率不如 AV1/HEVC。",
    },
    {
        "id": "h264-small", "label": "H.264 · 小体积", "codec": "h264", "quality": "small",
        "audio": "aac", "badge": "",
        "desc": "H.264 里的激进档，适合小屏观看。",
    },
    # ---------------- 降分辨率（可选）----------------
    {
        "id": "hevc-1080p", "label": "HEVC · 最高 1080p", "codec": "hevc", "quality": "balanced",
        "audio": "copy", "badge": "降分辨率",
        "desc": "超过 1080p 的片源缩到 1080p 再压，4K 素材体积可降一个数量级。低于 1080p 保持不变。",
        "max_height": 1080,
    },
    {
        "id": "hevc-720p", "label": "AV1 · 最高 720p", "codec": "av1", "quality": "balanced",
        "audio": "aac", "badge": "降分辨率",
        "desc": "移动端观看专用，体积最小。低码率片源压缩收益有限。",
        "max_height": 720,
    },
    # ---------------- 特殊 ----------------
    {
        "id": "remux", "label": "仅重封装（不重编码）", "codec": None, "quality": None,
        "audio": "copy", "badge": "秒级",
        "desc": "只更换容器、附加字幕、清理不需要的音轨，画质零损失，速度极快。",
    },
    {
        "id": "custom", "label": "手动配置", "codec": None, "quality": "custom",
        "audio": "copy", "badge": "手动", "manual": True,
        "desc": "自己指定编码格式、CRF 或目标码率、编码速度、分辨率上限、位深与音频编码。",
    },
]

PRESET_MAP = {p["id"]: p for p in PRESETS}


def get_preset(preset_id: str, manual: dict[str, Any] | None = None) -> dict[str, Any]:
    """按 id 取预设；手动配置在这里被投影成等价的参数结构。

    这样上层（扫描、预估、转码）完全不需要区分"预设"和"手动"。
    """
    p = PRESET_MAP.get(preset_id) or PRESET_MAP[DEFAULT_PRESET_ID]
    if p.get("manual"):
        return manual_preset(normalize_manual(manual))
    return p


# ---------------------------------------------------------------- 参数构造
def _audio_args(mode: str, channels: int) -> list[str]:
    if mode == "copy":
        return ["-c:a", "copy"]
    kbps = AUDIO_BITRATE_BY_CH.get(channels, 192)
    if mode == "opus":
        return ["-c:a", "libopus", "-b:a", f"{max(64, int(kbps * 0.75))}k"]
    # 默认 aac
    return ["-c:a", "aac", "-b:a", f"{kbps}k"]


def _encoder_preset(kind: str, name: str, quality: str, speed: str | None) -> str:
    """速度档位：手动配置给的是五档之一，预设走原来的质量档映射。"""
    if name == "libsvtav1":
        return str(SPEED_TABLE["svtav1"].get(speed, SVT_PRESET.get(quality, 6)))
    if name == "libx265":
        return SPEED_TABLE["x265"].get(speed, X265_PRESET.get(quality, "medium"))
    if name == "libx264":
        return SPEED_TABLE["x264"].get(speed, X264_PRESET.get(quality, "medium"))
    if kind == "intel":
        return SPEED_TABLE["qsv"].get(speed, QSV_PRESET.get(quality, "medium"))
    if kind == "nvidia":
        return SPEED_TABLE["nvenc"].get(speed, NVENC_PRESET.get(quality, "p5"))
    if kind == "amd":
        return SPEED_TABLE["amf"].get(speed, "balanced")
    return ""


def build_video_args(
    preset: dict[str, Any],
    encoder: dict[str, Any],
    media: dict[str, Any],
    custom_crf: float | None = None,
) -> dict[str, Any]:
    """返回 {pre: [...], vf_extra: str|None, vcodec: [...], out_extra: [...]}。

    encoder: {"name": "hevc_qsv", "codec": "hevc", "kind": "intel"}

    两种码率控制方式都支持：
    - 恒定质量（CRF/CQP/QP）——体积不可控但画质稳定，默认
    - 目标码率（VBR）——体积可控，适合有硬性空间预算的场景
    """
    codec = encoder["codec"]
    kind = encoder["kind"]
    name = encoder["name"]
    quality = preset.get("quality") or "balanced"
    video = media["video"]

    depth = preset.get("bit_depth") or "auto"
    if depth == "10":
        want_10bit = True
    elif depth == "8":
        want_10bit = False
    else:
        want_10bit = bool(preset.get("force_10bit")) or video.get("bit_depth", 8) >= 10
    hdr = bool(video.get("hdr"))
    anime = preset.get("tune") == "anime"

    # 仅重封装
    if preset.get("codec") is None:
        return {"pre": [], "vf_extra": None, "vcodec": ["-c:v", "copy"], "out_extra": [],
                "want_10bit": want_10bit, "crf": None, "mode": "copy", "target_kbps": 0}

    if custom_crf is not None:
        crf = float(custom_crf)
    elif quality == "custom" and preset.get("crf") is not None:
        crf = float(preset["crf"])
    else:
        crf = float(CRF_TABLE[codec].get(quality, 25))
    qp = int(round(crf + HW_QP_OFFSET)) if kind != "cpu" else int(round(crf))

    target_kbps = int(preset.get("bitrate_kbps") or 0)
    speed = preset.get("speed")
    sp = _encoder_preset(kind, name, quality, speed)

    bs = f"{target_kbps}k"
    mx = f"{int(target_kbps * 1.5)}k"
    bf = f"{int(target_kbps * 2)}k"

    pre: list[str] = []
    vf_extra: str | None = None
    vcodec: list[str] = []
    out_extra: list[str] = []

    if kind == "cpu":
        if name == "libsvtav1":
            vcodec = ["-c:v", "libsvtav1", "-preset", sp]
            vcodec += ["-b:v", bs] if target_kbps else ["-crf", str(int(crf))]
            svt = ["tune=0", "film-grain=8"]
            if anime:
                svt = ["tune=0", "film-grain=0", "enable-qm=1"]
            vcodec += ["-svtav1-params", ":".join(svt)]
        elif name == "libaom-av1":
            vcodec = ["-c:v", "libaom-av1", "-cpu-used", "6", "-row-mt", "1"]
            vcodec += ["-b:v", bs] if target_kbps else ["-crf", str(int(crf)), "-b:v", "0"]
        elif name == "libx265":
            params = ["log-level=error", "no-info=1"]
            if anime:
                params.append("psy-rd=1.2")
            if hdr or want_10bit:
                params.append("hdr-opt=1")
            vcodec = ["-c:v", "libx265", "-preset", sp]
            vcodec += (["-b:v", bs, "-maxrate", mx, "-bufsize", bf] if target_kbps
                       else ["-crf", str(int(crf))])
            vcodec += ["-x265-params", ":".join(params)]
            if want_10bit:
                vcodec += ["-pix_fmt", "yuv420p10le"]
        elif name == "libx264":
            vcodec = ["-c:v", "libx264", "-preset", sp]
            vcodec += (["-b:v", bs, "-maxrate", mx, "-bufsize", bf] if target_kbps
                       else ["-crf", str(int(crf))])
            vcodec += ["-profile:v", "high", "-pix_fmt", "yuv420p"]
            if anime:
                vcodec += ["-tune", "animation"]

    elif kind == "intel":  # QSV
        vcodec = ["-c:v", name, "-preset", sp]
        if target_kbps:
            vcodec += ["-b:v", bs, "-maxrate", mx, "-bufsize", bf]
        else:
            vcodec += ["-global_quality", str(qp)]
            if codec in ("hevc", "h264"):
                vcodec += ["-look_ahead", "1"]
        # AV1 的 profile 只有 main（8/10bit 同 profile），不能设 main10
        if want_10bit and codec == "hevc":
            vcodec += ["-profile:v", "main10"]

    elif kind == "nvidia":  # NVENC
        vcodec = ["-c:v", name, "-preset", sp, "-tune", "hq"]
        if target_kbps:
            vcodec += ["-rc", "vbr", "-b:v", bs, "-maxrate", mx, "-bufsize", bf]
        else:
            vcodec += ["-rc", "vbr", "-cq", str(qp), "-b:v", "0", "-multipass", "qres"]
        if codec == "h264":
            vcodec += ["-profile:v", "high"]
        if want_10bit and codec == "hevc":
            vcodec += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        elif want_10bit and codec == "av1":
            vcodec += ["-pix_fmt", "p010le"]

    elif kind == "amd":  # AMF（Linux 上一般不可用，保留）
        if target_kbps:
            vcodec = ["-c:v", name, "-quality", sp, "-rc", "vbr_peak", "-b:v", bs]
        else:
            vcodec = ["-c:v", name, "-quality", sp, "-rc", "cqp",
                      "-qp_i", str(qp), "-qp_p", str(qp)]

    elif kind == "vaapi":
        pre = ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}",
               "-filter_hw_device", "va"]
        fmt = "p010le" if (want_10bit and codec in ("hevc", "av1")) else "nv12"
        vf_extra = f"format={fmt},hwupload"
        if target_kbps:
            vcodec = ["-c:v", name, "-b:v", bs, "-maxrate", mx, "-rc_mode", "VBR"]
        else:
            vcodec = ["-c:v", name, "-qp", str(qp), "-rc_mode", "CQP"]

    return {"pre": pre, "vf_extra": vf_extra, "vcodec": vcodec, "out_extra": out_extra,
            "want_10bit": want_10bit,
            "crf": None if target_kbps else crf,
            "mode": "bitrate" if target_kbps else "crf",
            "target_kbps": target_kbps}


def scale_filter(max_height: int | None, video: dict[str, Any]) -> str | None:
    """仅在源分辨率高于目标时缩放，且保证宽高为偶数。"""
    if not max_height:
        return None
    h = int(video.get("height") or 0)
    if h <= max_height:
        return None
    # -2 保证宽度自动取偶数
    return f"scale=-2:{max_height}"


def _anchor_to_source(theory: float, video: dict[str, Any], dst_codec: str) -> float:
    """用源码率给「理论码率」做锚定。

    只按分辨率×帧率算的理论值，在两个方向上都会失真：

    - 源是 3 Mbps 的 1080p H.264：理论值算出 3.2 Mbps，界面显示「体积还变大」。
      但这种片源本身就是压过的，HEVC CRF21 实际通常还能省 10~30%。
    - 源是 20 Mbps 的 1080p 高码率母带：理论值同样算出 3.2 Mbps，严重低估。

    根因：CRF 编码的输出码率由**画面复杂度**决定，而同分辨率下源码率正是复杂度最直接的
    代理（码率高 ≈ 细节多 / 噪点重）。所以取两者的几何平均——既保留编码器效率给的理论
    下限，又跟随片源的实际复杂度，两端都不至于离谱。
    """
    src = float(video.get("bitrate") or 0)
    if src <= 0:
        return theory
    anchored = src * codec_efficiency(video.get("codec"), dst_codec)
    if anchored <= 0:
        return theory
    est = math.sqrt(theory * anchored)
    # 重编码不会凭空造出码率：源码率是硬上限（留 2% 给容器与封装开销）
    return min(est, src * 1.02)


def estimate_size(
    media: dict[str, Any],
    preset: dict[str, Any],
    encoder_codec: str | None,
    custom_crf: float | None = None,
    audio_tracks: list[int] | None = None,
) -> dict[str, Any]:
    """估算压缩后体积（字节）。纯经验公式，仅用于给用户一个数量级参考。

    audio_tracks: 用户选中的音轨（源文件流 index 列表）；None 表示全部保留。
    """
    video = media["video"]
    duration = float(media.get("duration") or 0)
    if duration <= 0:
        return {"bytes": 0, "bitrate": 0, "ratio": 0, "confidence": "low"}

    if preset.get("codec") is None:  # remux：只换容器/加字幕/裁音轨
        dropped = 0
        if audio_tracks is not None:
            keep = set(audio_tracks)
            removed = [a for a in (media.get("audio") or []) if a.get("index") not in keep]
            dropped = sum(int(a.get("bitrate") or 0) for a in removed) * duration / 8
        total = max(0.0, media["size"] * 0.995 - dropped)
        return {"bytes": int(total), "bitrate": int(media["total_bitrate"]),
                "ratio": round(total / media["size"], 3) if media["size"] else 1.0, "confidence": "high"}

    codec = encoder_codec or preset.get("codec") or "hevc"
    quality = preset.get("quality") or "balanced"
    target_kbps = int(preset.get("bitrate_kbps") or 0)

    w = int(video.get("width") or 1920)
    h = int(video.get("height") or 1080)
    if preset.get("max_height") and h > preset["max_height"]:
        ratio = preset["max_height"] / h
        w, h = w * ratio, preset["max_height"]

    fps = float(video.get("fps") or 25)
    fps = min(max(fps, 12), 120)

    if target_kbps:
        # 目标码率模式下体积是"算出来的"，不是估的
        v_bitrate = float(target_kbps * 1000)
    else:
        if custom_crf is not None:
            eff_crf = float(custom_crf)
        elif quality == "custom" and preset.get("crf") is not None:
            eff_crf = float(preset["crf"])
        else:
            eff_crf = float(CRF_TABLE.get(codec, CRF_TABLE["hevc"]).get(quality, 25))

        if quality == "custom":
            # 把 CRF 粗略换算成 bpp：以 balanced 为基准，每 +6 CRF 约减半
            base_bpp = BPP.get(codec, BPP["hevc"])["balanced"]
            bpp = base_bpp * (2 ** ((CRF_TABLE[codec]["balanced"] - eff_crf) / 6))
        else:
            bpp = BPP.get(codec, BPP["hevc"]).get(quality, 0.042)

        # 该质量档下编码器的"理论"码率（1.15：实拍噪点的保守上浮），
        # 再按源码率锚定——只按分辨率算会无视片源本身已经被压过的事实
        theory = bpp * w * h * fps * 1.15
        v_bitrate = _anchor_to_source(theory, video, codec)

    audio_mode = preset.get("audio", "copy")
    audios = list(media.get("audio") or [])
    if audio_tracks is not None:
        keep = set(audio_tracks)
        audios = [a for a in audios if a.get("index") in keep]

    manual_kbps = int(preset.get("audio_kbps") or 0)
    if not audios:
        a_bitrate = 0
    elif audio_mode == "copy" and not manual_kbps:
        a_bitrate = sum(int(a.get("bitrate") or 0) for a in audios)
        if a_bitrate <= 0:
            a_bitrate = 128_000 * len(audios)
    elif manual_kbps:
        a_bitrate = manual_kbps * 1000 * len(audios)
    else:
        ch = max((a.get("channels") or 2) for a in audios)
        a_bitrate = AUDIO_BITRATE_BY_CH.get(ch, 192) * 1000

    total_bytes = (v_bitrate + a_bitrate) * duration / 8
    orig = media["size"] or 1
    return {
        "bytes": int(total_bytes),
        "bitrate": int(v_bitrate + a_bitrate),
        "ratio": round(total_bytes / orig, 3),
        "confidence": "high" if target_kbps else "medium",
    }
