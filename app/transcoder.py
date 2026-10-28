"""转码引擎：命令构建、队列调度、进度解析、取消。"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from typing import Any

from starlette.concurrency import run_in_threadpool

from . import config, presets, probe as probe_mod, scanner
from .hwdetect import probe as hw

LANG_MAP = {
    "zh": "chi", "chs": "chi", "cht": "chi", "chi": "chi", "sc": "chi", "tc": "chi",
    "chinese": "chi", "简体": "chi", "繁体": "chi", "中字": "chi",
    "en": "eng", "eng": "eng", "english": "eng",
    "ja": "jpn", "jp": "jpn", "jpn": "jpn", "japanese": "jpn",
    "ko": "kor", "kor": "kor", "korean": "kor",
    "fr": "fra", "de": "deu", "es": "spa", "ru": "rus", "it": "ita", "pt": "por",
}

SUB_CODEC_BY_EXT = {
    ".srt": "subrip", ".ass": "ass", ".ssa": "ass", ".vtt": "webvtt",
    ".sup": "hdmv_pgs_subtitle", ".idx": "dvd_subtitle", ".sub": "dvd_subtitle",
    ".smi": "sami",
}

GRAPHIC_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

# 列表接口不必下发的字段：cmd_preview 是整条 ffmpeg 命令行（可达 2KB/条），
# subtitles / manual 也只有详情才用得到。300 条任务光命令行就能到 600KB。
LIGHT_SKIP = {"cmd_preview", "subtitles", "manual"}

# 显式点名硬件编码时的界面叫法，用于报错文案
HW_PREF_LABEL = {"intel": "Intel 核显", "nvidia": "NVIDIA NVENC", "amd": "AMD AMF"}


@dataclass
class Job:
    id: str
    src: str
    preset_id: str = presets.DEFAULT_PRESET_ID
    preference: str = "auto"
    subtitle_mode: str = "keep"          # keep（软字幕轨）| none
    subtitles: list[str] = field(default_factory=list)   # 用户勾选的外挂字幕
    audio_mode: str | None = None        # 覆盖预设：copy | aac | opus
    audio_tracks: list[int] | None = None  # 保留的音轨（源流 index）；None = 全部
    custom_crf: float | None = None
    manual: dict[str, Any] | None = None   # 「手动配置」预设的参数
    out: str = ""
    container: str = ""
    encoder: str = ""
    encoder_kind: str = ""
    hw_note: str = ""                    # 走 CPU 软件编码时的提示（硬件不可用）
    status: str = "queued"               # queued | running | done | failed | canceled
    progress: float = 0.0
    speed: float = 0.0
    fps: float = 0.0
    out_time: float = 0.0
    duration: float = 0.0
    eta: float = 0.0
    message: str = ""
    cmd_preview: str = ""
    size_before: int = 0
    size_after: int = 0
    source_action: str = ""              # "" | keep | rename | delete
    source_note: str = ""
    source_target: str = ""              # rename 后的新路径
    renamed_to: str = ""                 # 输出改名为源文件名后的路径
    rename_note: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    _proc: Any = field(default=None, repr=False, compare=False)

    def public(self, light: bool = False) -> dict[str, Any]:
        # 手工构造，避免 asdict 深拷贝 asyncio 进程对象
        d: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "_proc":
                continue
            if light and f.name in LIGHT_SKIP:
                continue
            d[f.name] = getattr(self, f.name)
        if not light and d.get("cmd_preview") and len(d["cmd_preview"]) > 2000:
            d["cmd_preview"] = d["cmd_preview"][:2000] + " …"
        d["ratio"] = round(self.size_after / self.size_before, 3) if self.size_before and self.size_after else None
        return d


def _lang_from_name(filename: str, video_stem: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    tail = stem[len(video_stem):].strip("._- ") if stem.startswith(video_stem) else stem
    for token in re.split(r"[._\-\[\]() ]+", tail.lower()):
        if token in LANG_MAP:
            return LANG_MAP[token]
    lowered = tail.lower()
    for key, val in LANG_MAP.items():
        if key in lowered:
            return val
    return ""


MP4_FAMILY = ("mp4", "mov", "m4a", "3gp", "3g2", "mj2", "psp")

# webm 只允许 VP8 / VP9 / AV1 视频
WEBM_VIDEO_OK = {"vp8", "vp9", "av1"}


def decide_container(media: dict, subtitle_codecs: list[str], video_codec: str | None = None) -> str:
    """决定输出容器。

    config.OUTPUT_CONTAINER 是用户意图（默认 mkv）；auto 表示跟随源文件。
    当目标容器装不下内容时自动退回 mkv —— mkv 是唯一什么都能装的容器，
    宁可换个后缀，也不要为了塞进 mp4 去牺牲音频或字幕。
    """
    target = (config.OUTPUT_CONTAINER or "mkv").lower()
    src = (media.get("container") or "").lower()
    has_graphic = any(c in GRAPHIC_SUBS for c in subtitle_codecs)

    if target == "auto":
        if has_graphic:
            return "mkv"  # 图形字幕只有 mkv 能装
        if src in MP4_FAMILY:
            return "mp4"
        if src == "webm":
            return "webm"
        return "mkv"

    if target == "mp4":
        if has_graphic:
            return "mkv"  # mp4 只能存文本字幕，PGS/DVD 会直接让任务失败
        return "mp4"

    if target == "webm":
        codec = (video_codec or (media.get("video") or {}).get("codec") or "").lower()
        if codec in WEBM_VIDEO_OK and not has_graphic:
            return "webm"
        return "mkv"  # HEVC/H.264 装不进 webm

    return "mkv"  # 默认，也是所有未知值的兜底


def _audio_args_for_container(
    audios: list[dict], audio_mode: str, container: str, audio_kbps: int | None = None
) -> list[str]:
    """只为已选中的音轨生成编码参数。audio_kbps 为手动配置里的音频码率。"""
    if not audios:
        return ["-an"]
    if audio_mode == "copy":
        ok = True
        for a in audios:
            codec = (a.get("codec") or "").lower()
            if container == "mp4" and codec not in probe_mod.MP4_AUDIO_OK:
                ok = False
            if container == "webm" and codec not in probe_mod.WEBM_AUDIO_OK:
                ok = False
        if ok:
            return ["-c:a", "copy"]
        # 容器不兼容，降级转码
        ch = max((a.get("channels") or 2) for a in audios)
        kbps = audio_kbps or presets.AUDIO_BITRATE_BY_CH.get(ch, 192)
        if container == "webm":
            return ["-c:a", "libopus", "-b:a", f"{max(64, int(kbps * 0.75))}k"]
        return ["-c:a", "aac", "-b:a", f"{kbps}k"]
    ch = max((a.get("channels") or 2) for a in audios)
    kbps = audio_kbps or presets.AUDIO_BITRATE_BY_CH.get(ch, 192)
    if audio_mode == "opus":
        return ["-c:a", "libopus", "-b:a", f"{max(64, int(kbps * 0.75))}k"]
    return ["-c:a", "aac", "-b:a", f"{kbps}k"]


def build_command(job: Job, media: dict) -> tuple[list[str], dict[str, Any]]:
    preset = presets.get_preset(job.preset_id, job.manual)
    video = media["video"]
    stem = os.path.splitext(os.path.basename(job.src))[0]

    # ---- 字幕：只做软字幕轨，永不烧录进画面（烧录会二次损失画质）
    keep_subs = job.subtitle_mode != "none"
    ext_subs = [p for p in job.subtitles if os.path.exists(p)] if keep_subs else []
    sub_codecs = [SUB_CODEC_BY_EXT.get(os.path.splitext(p)[1].lower(), "subrip") for p in ext_subs]
    embedded = (media.get("subtitles") or []) if keep_subs else []

    container = decide_container(
        media,
        [s["codec"] for s in embedded] + sub_codecs,
        preset.get("codec") or video.get("codec"),
    )
    job.container = container
    if not job.out:
        job.out = make_output_path(job.src, container)

    # ---- 音轨：用户勾选的集合，None 表示全部保留
    all_audio = media.get("audio") or []
    if job.audio_tracks is None:
        audio_sel = list(all_audio)
    else:
        wanted = set(job.audio_tracks)
        audio_sel = [a for a in all_audio if a.get("index") in wanted]

    # ---- 选编码器
    codec_family = preset.get("codec")
    if codec_family:
        enc = hw.pick(codec_family, job.preference)
        if enc is None:
            raise RuntimeError(f"没有可用的 {codec_family} 编码器，请点顶部硬件徽章的「详情」查看实测结果")
        # 用户明确点名了硬件，却只能退回软件编码时直接失败：
        # 否则会安静地用 CPU 跑几小时，用户还以为核显在干活
        if enc["kind"] == "cpu" and job.preference in ("intel", "nvidia", "amd"):
            label = HW_PREF_LABEL[job.preference]
            raise RuntimeError(
                f"你选了「{label}」，但这台机器上没有它可用的编码器。"
                "点顶部硬件徽章的「详情」能看到每个编码器的实测报错；"
                "想先用 CPU 跑就把编码器改成「自动」或「仅 CPU」"
            )
        if enc["kind"] == "cpu":
            job.hw_note = "本次为 CPU 软件编码（硬件编码不可用，见顶部硬件徽章「详情」）"
    else:
        enc = {"name": "copy", "codec": "copy", "kind": "cpu"}
    job.encoder = enc["name"]
    job.encoder_kind = enc["kind"]

    va = presets.build_video_args(preset, enc, media, job.custom_crf)

    cmd: list[str] = [config.FFMPEG, "-hide_banner", "-y", "-nostdin", "-nostats", "-progress", "pipe:1"]
    cmd += va["pre"]
    cmd += ["-i", job.src]
    for sub in ext_subs:
        cmd += ["-i", sub]

    # ---- 流映射（音轨逐条精确映射，不再整组复制）
    cmd += ["-map", "0:v:0"]
    for a in audio_sel:
        cmd += ["-map", f"0:{a['index']}"]
    if keep_subs and embedded:
        cmd += ["-map", "0:s?"]
    for i, _ in enumerate(ext_subs):
        # 不加 ? ：让 map 失败时直接报错，避免字幕轨索引错位导致后续 metadata 打错语言
        cmd += ["-map", f"{i + 1}:s:0"]

    # ---- 视频
    filters: list[str] = []
    scale = presets.scale_filter(preset.get("max_height"), video)
    if scale:
        filters.append(scale)
    if va["vf_extra"]:
        filters.append(va["vf_extra"])
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += va["vcodec"]

    # ---- 音频
    audio_mode = job.audio_mode or preset.get("audio", "copy")
    cmd += _audio_args_for_container(audio_sel, audio_mode, container, preset.get("audio_kbps"))

    # 音轨的语言/标题显式写回，避免转码后播放器显示"未知语言"
    for i, a in enumerate(audio_sel):
        if a.get("language"):
            cmd += [f"-metadata:s:a:{i}", f"language={a['language']}"]
        if a.get("title"):
            cmd += [f"-metadata:s:a:{i}", f"title={a['title']}"]

    # ---- 字幕编码
    sub_count = len(embedded) + len(ext_subs)
    if sub_count:
        if container == "mp4":
            cmd += ["-c:s", "mov_text"]
        elif container == "webm":
            cmd += ["-c:s", "webvtt"]
        else:
            cmd += ["-c:s", "copy"]

    # ---- 外挂字幕语言标签
    for i, sub in enumerate(ext_subs):
        lang = _lang_from_name(sub, stem)
        if lang:
            cmd += [f"-metadata:s:s:{len(embedded) + i}", f"language={lang}"]

    # ---- 容器相关
    if container == "mp4":
        cmd += ["-movflags", "+faststart"]
        if preset.get("codec") == "hevc" or video.get("codec") == "hevc":
            cmd += ["-tag:v", "hvc1"]
        if preset.get("codec") == "av1":
            cmd += ["-tag:v", "av01"]
    cmd += ["-max_muxing_queue_size", "4096"]
    cmd += ["-map_metadata", "0"]
    if container != "mp4":
        cmd += ["-map_chapters", "0"]
    cmd += [job.out]

    meta = {
        "preset": preset["id"],
        "preset_label": preset["label"],
        "container": container,
        "audio_mode": audio_mode,
        "audio_track_count": len(audio_sel),
        "subtitle_count": sub_count,
        "burn": False,
        "crf": va.get("crf"),
        "rate_mode": va.get("mode"),
        "target_kbps": va.get("target_kbps"),
        "filters": filters,
    }
    return cmd, meta


# 已被任务预定、但 ffmpeg 还没创建出来的输出路径
_reserved: set[str] = set()
_reserved_lock = threading.Lock()


def make_output_path(src: str, ext: str = "mkv") -> str:
    """分配一个还没被占用的输出路径，并当场「预定」下来。

    只判断 os.path.exists 是不够的：两个 worker 同时启动同一目录下的两条任务时，
    双方的输出文件都还没被 ffmpeg 创建出来，于是会算出同一个路径，最后互相覆盖
    （mkv 的索引表直接写坏）。这里用进程内的预定表 + 锁把「查重 + 占位」做成原子操作。
    """
    stem = os.path.splitext(os.path.basename(src))[0]
    if config.OUTPUT_DIR:
        base = config.OUTPUT_DIR
        os.makedirs(base, exist_ok=True)
    else:
        base = os.path.dirname(src)
    ext = ext.lstrip(".")
    with _reserved_lock:
        candidate = os.path.join(base, f"{stem}{config.OUTPUT_SUFFIX}.{ext}")
        n = 1
        while (
            os.path.exists(candidate)
            or os.path.abspath(candidate) == os.path.abspath(src)
            or candidate in _reserved
        ):
            candidate = os.path.join(base, f"{stem}{config.OUTPUT_SUFFIX}_{n}.{ext}")
            n += 1
        _reserved.add(candidate)
        return candidate


def release_output_path(path: str) -> None:
    """任务收尾后释放预定，避免预定表无限增长。"""
    if not path:
        return
    with _reserved_lock:
        _reserved.discard(path)


# ---------------------------------------------------------------- 源文件处理
SOURCE_ACTIONS = ("keep", "rename", "delete")


def archived_path(src: str) -> str:
    """movie.mkv -> movie.old.mkv（后缀插在扩展名前，播放器仍能直接打开）。"""
    base, ext = os.path.splitext(src)
    suffix = config.SOURCE_OLD_SUFFIX or ".old"
    src_real = os.path.realpath(src)
    candidate = f"{base}{suffix}{ext}"
    n = 1
    while os.path.exists(candidate) or os.path.realpath(candidate) == src_real:
        candidate = f"{base}{suffix}{n}{ext}"
        n += 1
    return candidate


def _source_guard(job: Job, action: str) -> str:
    """执行前的安全检查。返回拒绝原因；空串表示通过。

    这里的每一条都是"宁可不动手"——源文件删错了没法恢复。
    """
    if action not in SOURCE_ACTIONS:
        return f"未知操作：{action}"
    if job.status != "done":
        return "只有压缩成功的任务才能处理源文件"
    if job.source_action:
        return "这个任务的源文件已经处理过了"
    if not job.out or not os.path.isfile(job.out):
        return "输出文件不存在，为安全起见不动源文件"
    out_size = os.path.getsize(job.out)
    if out_size <= 0:
        return "输出文件是空的，为安全起见不动源文件"
    if not os.path.isfile(job.src):
        return "源文件已不在原路径（可能被手动移动或删除了）"
    if os.path.realpath(job.src) == os.path.realpath(job.out):
        return "源文件与输出文件是同一个文件，拒绝操作"
    # 输出体积小得离谱，基本可以确定是截断的失败产物
    # 阈值同时考虑绝对值和相对值，避免把"压得很好的小文件"误判成失败
    floor = max(200_000, int((job.size_before or 0) * 0.005))
    if out_size < floor:
        return (f"输出只有 {out_size / 1e6:.2f}MB（源 {job.size_before / 1e6:.2f}MB），"
                f"疑似不完整，请先播放确认再处理源文件")
    if action == "delete" and not config.ALLOW_SOURCE_DELETE:
        return "服务端已禁用删除（ALLOW_SOURCE_DELETE=0），请改用「改名归档」"
    return ""


def apply_source_action(job: Job, action: str) -> tuple[bool, str]:
    """对单个任务执行源文件处理，返回 (成功, 说明)。"""
    reason = _source_guard(job, action)
    if reason:
        return False, reason

    if action == "keep":
        job.source_action = "keep"
        job.source_note = "源文件已保留"
        return True, job.source_note

    try:
        if action == "rename":
            target = archived_path(job.src)
            os.rename(job.src, target)
            job.source_action = "rename"
            job.source_target = target
            job.source_note = f"源文件已改名归档：{os.path.basename(target)}"
            return True, job.source_note

        os.remove(job.src)
        job.source_action = "delete"
        job.source_note = "源文件已删除"
        return True, job.source_note
    except OSError as exc:
        return False, f"操作失败：{exc}"


def rename_output_to_source(job: Job) -> tuple[bool, str]:
    """把转码结果改回源文件名，只保留输出的扩展名（去掉 _compressed 之类后缀）。

    前提是源文件已经腾出原名：删掉了，或改名归档成 .old。
    源文件还在原位置时拒绝执行——否则 movie.mkv 会盖掉源文件。
    """
    if job.status != "done":
        return False, "只有压缩成功的任务才能改名"
    if job.renamed_to:
        return False, "这个输出文件已经改过名了"
    if not job.out or not os.path.isfile(job.out):
        return False, "输出文件不存在，无法改名"
    if job.source_action not in ("delete", "rename"):
        return False, "源文件还占着原名，先删除或改名归档，再改输出文件名"

    # job.src 始终是源文件的原路径（deleted/renamed 后也不改），stem 一定正确
    stem = os.path.splitext(os.path.basename(job.src))[0]
    ext = os.path.splitext(job.out)[1]
    base = os.path.dirname(job.out)
    target = os.path.join(base, f"{stem}{ext}")

    if os.path.abspath(target) == os.path.abspath(job.out):
        return False, "输出文件已经是源文件名，不需要改"
    if os.path.exists(target):
        return False, f"{os.path.basename(target)} 已存在，为避免覆盖已中止"

    old_name = os.path.basename(job.out)
    try:
        os.rename(job.out, target)
    except OSError as exc:
        return False, f"改名失败：{exc}"

    job.out = target
    job.renamed_to = target
    job.rename_note = f"输出已改名为 {os.path.basename(target)}"
    return True, f"{old_name} → {os.path.basename(target)}"


class JobManager:
    """单进程内的任务队列，N 个 worker 并发执行。"""

    KEEP = 300  # 列表里最多保留多少条任务

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self._started = False
        # 任务列表的版本号：任何字段变化都 +1。前端凭它判断要不要重绘，
        # 没变化时只回一个 changed:false，省掉 1.5 秒一次的全量传输
        self.rev = 0

    def touch(self) -> None:
        self.rev += 1

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(config.CONCURRENT_JOBS):
            self.workers.append(asyncio.create_task(self._worker(i), name=f"job-worker-{i}"))

    async def stop(self) -> None:
        # 先把正在跑的 ffmpeg 结束掉，否则容器退出后这些子进程会变成孤儿，
        # 继续占着显卡和 CPU 写文件
        for job in self.jobs.values():
            proc = job._proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        for _ in self.workers:
            await self.queue.put(None)
        for w in self.workers:
            w.cancel()
        self.workers.clear()
        self._started = False

    def add(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.order.append(job.id)
        self._trim()
        self.touch()
        self.queue.put_nowait(job.id)

    def _trim(self) -> None:
        """只淘汰「已结束」的任务，且必须同时从 jobs 和 order 里摘掉。

        老写法只从 order 弹、且遇到未结束的任务就直接 continue，
        结果 unfinished 的 id 被弹出 order 却还留在 jobs 里——界面上看不到、
        也不会再被执行，内存一直涨（实测 320 条任务里 20 条成了幽灵）。
        """
        while len(self.order) > self.KEEP:
            victim = None
            for idx, jid in enumerate(self.order):
                j = self.jobs.get(jid)
                if j is None or j.status in ("done", "failed", "canceled"):
                    victim = idx
                    break
            if victim is None:  # 满屏都是未结束任务，先不淘汰
                return
            jid = self.order.pop(victim)
            self.jobs.pop(jid, None)

    def has_active(self, src: str) -> bool:
        """这个源文件是不是已经有任务在排队/在跑。"""
        real = os.path.realpath(src)
        for jid in self.order:
            job = self.jobs.get(jid)
            if job and job.status in ("queued", "running") and os.path.realpath(job.src) == real:
                return True
        return False

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_public(self, light: bool = False) -> list[dict[str, Any]]:
        return [self.jobs[j].public(light=light) for j in self.order if j in self.jobs]

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status == "queued":
            job.status = "canceled"
            job.message = "已取消（未开始）"
            job.finished_at = time.time()
            self.touch()
            return True
        if job.status == "running" and job._proc:
            job.status = "canceled"
            job.message = "正在取消…"
            self.touch()
            try:
                job._proc.terminate()
            except ProcessLookupError:
                pass
            return True
        return False

    def source_action(self, job_ids: list[str], action: str) -> dict[str, Any]:
        """对一批任务执行源文件处理（保留 / 改名归档 / 删除）。"""
        results: list[dict[str, Any]] = []
        for jid in job_ids:
            job = self.jobs.get(jid)
            if not job:
                results.append({"id": jid, "ok": False, "message": "任务不存在"})
                continue
            ok, msg = apply_source_action(job, action)
            results.append({
                "id": jid, "ok": ok, "message": msg,
                "path": job.src, "target": job.source_target,
            })
        self.touch()
        return {
            "ok": bool(results) and all(r["ok"] for r in results),
            "ok_count": sum(1 for r in results if r["ok"]),
            "results": results,
        }

    def rename_outputs(self, job_ids: list[str]) -> dict[str, Any]:
        """把一批转码结果改回源文件名（去掉 _compressed 之类后缀）。"""
        results: list[dict[str, Any]] = []
        for jid in job_ids:
            job = self.jobs.get(jid)
            if not job:
                results.append({"id": jid, "ok": False, "message": "任务不存在"})
                continue
            ok, msg = rename_output_to_source(job)
            results.append({
                "id": jid, "ok": ok, "message": msg,
                "path": job.out, "target": job.renamed_to,
            })
        self.touch()
        return {
            "ok": bool(results) and all(r["ok"] for r in results),
            "ok_count": sum(1 for r in results if r["ok"]),
            "results": results,
        }

    def clear_finished(self) -> int:
        removed = 0
        for jid in list(self.order):
            job = self.jobs.get(jid)
            if job and job.status in ("done", "failed", "canceled"):
                self.jobs.pop(jid, None)
                self.order.remove(jid)
                removed += 1
        if removed:
            self.touch()
        return removed

    # ------------------------------------------------------------ worker
    async def _worker(self, idx: int) -> None:
        while True:
            job_id = await self.queue.get()
            if job_id is None:
                break
            job = self.jobs.get(job_id)
            if not job or job.status != "queued":
                continue
            try:
                await self._run(job)
            except asyncio.CancelledError:
                if job.status == "running":
                    job.status = "canceled"
                raise
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.message = str(exc)[:500]
                job.finished_at = time.time()
                self.touch()

    async def _run(self, job: Job) -> None:
        # ffprobe 是同步子进程，直接在这里调会把整个事件循环钉住一秒钟以上：
        # 期间所有接口（包括进度轮询）都停摆。丢到线程池里等。
        media = await run_in_threadpool(scanner.probe_cached, job.src)
        job.duration = float(media.get("duration") or 0)
        job.size_before = media["size"]

        cmd, meta = build_command(job, media)
        job.cmd_preview = " ".join(_quote(c) for c in cmd)
        job.status = "running"
        job.started_at = time.time()
        job.message = f"{meta['preset_label']} · {job.encoder} · {meta['container']}"
        self.touch()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            job._proc = proc
            stderr_tail: list[str] = []
            err_task = asyncio.create_task(_drain(proc.stderr, stderr_tail))

            try:
                await self._pump_progress(job, proc, stderr_tail)
            finally:
                try:
                    await asyncio.wait_for(err_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    err_task.cancel()

            code = await proc.wait()
            if code == 0 and os.path.exists(job.out):
                job.size_after = os.path.getsize(job.out)
                job.status = "done"
                job.progress = 100.0
                if job.size_before and job.size_after:
                    saved = 1 - job.size_after / job.size_before
                    delta = f"{_human(job.size_before)} → {_human(job.size_after)}"
                    if saved >= 0.001:
                        tail = f"省 {saved * 100:.1f}%"
                    elif saved <= -0.001:
                        tail = f"体积增加 {-saved * 100:.1f}%（容器开销）"
                    else:
                        tail = "体积基本持平"
                    job.message = f"完成 · {delta}（{tail}）"
                else:
                    job.message = "完成"
            elif job.status == "canceled" or code in (-15, 143, -9, 137):
                job.status = "canceled"
                job.message = "已取消"
                _cleanup(job.out)
            else:
                job.status = "failed"
                tail = [l for l in stderr_tail if l.strip()][-3:]
                job.message = (" / ".join(tail) or f"ffmpeg 退出码 {code}")[:500]
                _cleanup(job.out)
            job.finished_at = time.time()
        finally:
            job._proc = None
            release_output_path(job.out)
            self.touch()

    async def _pump_progress(self, job: Job, proc, stderr_tail: list[str]) -> None:
        state: dict[str, str] = {}
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                text = line.decode("utf-8", "ignore").strip()
            except Exception:  # noqa: BLE001
                continue
            if "=" not in text:
                continue
            key, _, val = text.partition("=")
            state[key] = val
            if key != "progress":
                continue
            if job.status == "canceled":
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                break
            self._apply_state(job, state)
            if val == "end":
                break

    def _apply_state(self, job: Job, state: dict[str, str]) -> None:
        try:
            out_us = int(state.get("out_time_us") or state.get("out_time_ms") or 0)
        except ValueError:
            out_us = 0
        job.out_time = out_us / 1_000_000
        if job.duration > 0:
            job.progress = max(0.0, min(99.9, job.out_time / job.duration * 100))
        speed_txt = (state.get("speed") or "").strip().rstrip("x")
        try:
            job.speed = float(speed_txt) if speed_txt else 0.0
        except ValueError:
            job.speed = 0.0
        try:
            job.fps = float(state.get("fps") or 0)
        except ValueError:
            job.fps = 0.0
        if job.speed > 0 and job.duration > job.out_time:
            job.eta = (job.duration - job.out_time) / job.speed
        # 进度变了也算「有变化」，否则前端凭 rev 判断会一直拿到 changed:false，
        # 进度条就停在原地不动了
        self.touch()


async def _drain(stream, sink: list[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        try:
            sink.append(line.decode("utf-8", "ignore").rstrip())
        except Exception:  # noqa: BLE001
            continue
        if len(sink) > 60:
            del sink[:20]


def _quote(s: str) -> str:
    return f'"{s}"' if (" " in s or "'" in s) else s


def _cleanup(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _human(n: int) -> str:
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step:
            return f"{v:.1f}{unit}" if unit != "B" else f"{int(v)}B"
        v /= step
    return f"{v:.1f}PB"


def new_job(src: str, **kwargs) -> Job:
    return Job(id=uuid.uuid4().hex[:12], src=src, **kwargs)


manager = JobManager()
