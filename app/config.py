"""全局配置：全部通过环境变量覆盖，容器里改 env 即可。"""
from __future__ import annotations

import os
import shutil

APP_NAME = "MediaShrink"
APP_VERSION = "1.1.1"

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

# ---------------------------------------------------------------- 访问控制
# 访问令牌。留空 = 不校验（默认，适合只在本机/可信内网用）。
# 填了之后所有接口和页面都要带令牌：浏览器用 /?token=xxx 打开一次（之后写 Cookie），
# 脚本用 -H "X-Auth-Token: xxx"。这个服务能删源文件、能读整个媒体库，
# 一旦把端口暴露到内网以外，务必设上。
ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or os.environ.get("MS_TOKEN") or "").strip()

# 令牌 Cookie 的有效期（天）。SameSite=Strict，跨站请求带不上来，顺带把 CSRF 挡掉
TOKEN_COOKIE_DAYS = max(1, int(os.environ.get("TOKEN_COOKIE_DAYS", "30")))
TOKEN_COOKIE_NAME = "ms_token"

# 单次请求最多提交多少个任务（防止一次点错把整个库排进队列）
MAX_JOBS_PER_REQUEST = max(1, int(os.environ.get("MAX_JOBS_PER_REQUEST", "200")))

# ffprobe 结果缓存条数上限。缓存键是文件路径，长期运行 + 大媒体库会一直涨，
# 走 LRU 淘汰，只留最近用到的
PROBE_CACHE_MAX = max(100, int(os.environ.get("PROBE_CACHE_MAX", "4000")))

# 扫描/预估用的 ffprobe 线程数（全局共享，多个请求不会各自再开一份）
PROBE_WORKERS = max(1, int(os.environ.get("PROBE_WORKERS", "0")) or min(6, (os.cpu_count() or 4)))

# /api/hw/report 的冷却秒数：这个接口会实测四个编码器，很重，不允许连点
HW_REPORT_COOLDOWN = max(0, int(os.environ.get("HW_REPORT_COOLDOWN", "15")))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
