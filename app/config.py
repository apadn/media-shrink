"""全局配置：全部通过环境变量覆盖，容器里改 env 即可。"""
from __future__ import annotations

import os
import shutil

APP_NAME = "MediaShrink"
APP_VERSION = "1.0.5"

PORT = int(os.environ.get("PORT", "8000"))


def _resolve_bin(env_key: str, name: str, fallback: str) -> str:
    explicit = os.environ.get(env_key)
    if explicit:
        return explicit
    found = shutil.which(name)
    if found:
        return found
    return fallback


# jellyfin-ffmpeg 默认装在 /usr/lib/jellyfin-ffmpeg 下
FFMPEG = _resolve_bin("FFMPEG_BIN", "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFPROBE = _resolve_bin("FFPROBE_BIN", "ffprobe", "/usr/lib/jellyfin-ffmpeg/ffprobe")

# 允许浏览/扫描的根目录（安全边界，防止越权读宿主目录）
MEDIA_ROOTS = [p.strip() for p in os.environ.get("MEDIA_ROOTS", "/media,/data,/output").split(",") if p.strip()]

# 输出目录：留空表示"与原文件同目录"
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "").strip()

# 输出文件名后缀，默认 _compressed；避免覆盖源文件
OUTPUT_SUFFIX = os.environ.get("OUTPUT_SUFFIX", "_compressed")

# 输出容器：mkv（默认）| mp4 | webm | auto（跟随源文件容器）
# mkv 兼容性最好——任意视频编码、任意音频编码、PGS/ASS 等图形与特效字幕都能装，
# 而且默认不会因为容器限制去重编码音频。装不下的情况会自动退回 mkv。
OUTPUT_CONTAINER = os.environ.get("OUTPUT_CONTAINER", "mkv").strip().lower()

# 并行转码任务数（硬编码器通常 1~2 更稳）
CONCURRENT_JOBS = max(1, int(os.environ.get("CONCURRENT_JOBS", "1")))

# VAAPI 渲染节点
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")

# 扫描时最小文件体积过滤（MB），避免把花絮小文件也列出来
MIN_SIZE_MB = float(os.environ.get("MIN_SIZE_MB", "1"))

# 扫描默认最大递归深度
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "8"))

# 扫描时跳过的目录（默认值，界面上还能临时加）。
# 逗号分隔，不区分大小写，支持 * ? 通配符；既能写目录名（@eaDir、花絮），
# 也能写相对扫描根目录的路径（movies/花絮、*/sample）。
# 以 . 开头的隐藏目录始终跳过，不用写在这里。
EXCLUDE_DIRS = [
    p.strip()
    for p in os.environ.get(
        "EXCLUDE_DIRS",
        "@eaDir,#recycle,#snapshot,@Recycle,@tmp,lost+found,"
        "__MACOSX,System Volume Information,node_modules",
    ).split(",")
    if p.strip()
]

# ---------------------------------------------------------------- 源文件处理
# 转码完成后对源文件的处理方式：keep（保留，默认）| rename（加后缀归档）| delete（删除）
# 界面上每个完成的任单独可选，这里只是默认值
SOURCE_ACTION = os.environ.get("SOURCE_ACTION", "keep").strip().lower()

# 归档后缀：movie.mkv -> movie.old.mkv（插在扩展名前，双击仍能播放）
SOURCE_OLD_SUFFIX = os.environ.get("SOURCE_OLD_SUFFIX", ".old")

# 是否允许删除源文件。设成 0 后界面的「删除」按钮会被服务端拒绝，只能改名归档
ALLOW_SOURCE_DELETE = os.environ.get("ALLOW_SOURCE_DELETE", "1") == "1"

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
