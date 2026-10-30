"""FastAPI 入口：REST API + 静态前端。"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, history, hwdetect, presets, scanner
from .hwdetect import probe as hw
from .transcoder import manager, new_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.warn_if_open()
    hw.start()
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)

# 先装鉴权，后装防缓存中间件（越晚注册越靠外层），保证 401/403 也带 no-store
auth.install(app)

if os.path.isdir(config.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.middleware("http")
async def frontend_cache_headers(request: Request, call_next):
    """让浏览器每次都回源校验前端资源。

    之前一个 Cache-Control 都不发，浏览器会按「启发式缓存」把 index.html / app.js 存下来，
    升级镜像后打开页面还是旧版：新按钮不出现、版本号也不显示，只能手动强刷。
    这里给页面和静态资源发 no-cache（仍可缓存，但每次必须用 ETag 校验，没变就是 304，开销可忽略），
    API 发 no-store，避免任务列表被中间层缓存。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/") or path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------- 模型
class ScanReq(BaseModel):
    path: str
    recursive: bool = True
    preset_id: str = presets.DEFAULT_PRESET_ID
    preference: str = "auto"
    custom_crf: float | None = None
    min_size_mb: float | None = None
    # 「手动配置」预设的参数；preset_id 不是 custom 时忽略
    manual: dict[str, Any] | None = None
    # 本次扫描临时排除的目录（glob；目录名或相对路径），叠加在 EXCLUDE_DIRS 之上
    exclude: list[str] | None = None
    # 忽略转码历史：勾上之后已经压过的文件不会再被标成「已转过」
    ignore_history: bool = False


class EstimateItem(BaseModel):
    path: str
    preset_id: str | None = None
    manual: dict[str, Any] | None = None
    custom_crf: float | None = None
    tracks: list[int] | None = None     # 音轨流 index；None = 全部


class EstimateReq(BaseModel):
    # 新形态：每个文件可带各自的手动配置
    items: list[EstimateItem] = Field(default_factory=list)
    preference: str = "auto"
    # 兼容旧的整批形态
    paths: list[str] = Field(default_factory=list)
    preset_id: str = presets.DEFAULT_PRESET_ID
    custom_crf: float | None = None
    manual: dict[str, Any] | None = None
    tracks: dict[str, list[int]] = Field(default_factory=dict)
    ignore_history: bool = False


class JobItem(BaseModel):
    path: str
    preset_id: str | None = None
    subtitles: list[str] = Field(default_factory=list)
    subtitle_mode: str = "keep"          # keep | none（不支持烧录）
    audio_mode: str | None = None
    audio_tracks: list[int] | None = None  # None = 全部音轨
    custom_crf: float | None = None
    manual: dict[str, Any] | None = None


class JobsReq(BaseModel):
    items: list[JobItem]
    preset_id: str = presets.DEFAULT_PRESET_ID
    preference: str = "auto"
    manual: dict[str, Any] | None = None


class SourceActionReq(BaseModel):
    job_ids: list[str] = Field(default_factory=list)
    action: str                       # keep | rename | delete
    confirm: bool = False             # 删除必须为 true


class RenameOutputReq(BaseModel):
    job_ids: list[str] = Field(default_factory=list)


class PauseReq(BaseModel):
    job_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 基础
@app.get("/", response_class=HTMLResponse)
async def index():
    """把 index.html 读出来，就地填上版本号再返回。

    版本号绝不交给前端 JS 二次渲染：只要浏览器里缓存着旧的 app.js（升级镜像后很常见），
    JS 就不会去取消 hidden，界面上永远看不到版本。这里由服务端直接写死文本，
    哪怕 app.js 一个字节都没加载，标题和胶囊也已经在 HTML 里了。
    """
    path = os.path.join(config.STATIC_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "前端文件缺失"}, status_code=500)
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace("{{APP_VERSION}}", config.APP_VERSION)
    return HTMLResponse(html)


# ---------------------------------------------------------------- 站点图标
# 浏览器/系统会直接请求根路径的 /favicon.ico、/apple-touch-icon.png（不看 HTML 里写了什么），
# 所以除了 /static 下的文件，这里也开四个根路径，省得访问日志里一直刷 404。
_ICONS = {
    "favicon.ico": ("favicon.ico", "image/x-icon"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
    "apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "apple-touch-icon-precomposed.png": ("apple-touch-icon.png", "image/png"),
}


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def site_icon(request: Request):
    name = os.path.basename(request.url.path)
    filename, media_type = _ICONS[name]
    path = os.path.join(config.STATIC_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "图标文件缺失")
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "ffmpeg": hw.state().get("ffmpeg", "")}


@app.get("/api/config")
async def get_config():
    return {
        "version": config.APP_VERSION,
        "media_roots": config.MEDIA_ROOTS,
        "output_dir": config.OUTPUT_DIR or "（与原文件同目录）",
        "output_suffix": config.OUTPUT_SUFFIX,
        "output_container": config.OUTPUT_CONTAINER,
        "source_action": config.SOURCE_ACTION,
        "source_old_suffix": config.SOURCE_OLD_SUFFIX,
        "allow_source_delete": config.ALLOW_SOURCE_DELETE,
        "concurrency": config.CONCURRENT_JOBS,
        "vaapi_device": config.VAAPI_DEVICE,
        "default_preset": presets.DEFAULT_PRESET_ID,
        "manual_preset": presets.MANUAL_PRESET_ID,
        "manual_defaults": presets.normalize_manual(None),
        "target_codecs": sorted(scanner.TARGET_CODECS.values()),
        "exclude_dirs": config.EXCLUDE_DIRS,
        "history_enabled": bool(config.HISTORY_FILE),
        "history_max": config.HISTORY_MAX,
    }


@app.get("/api/presets")
async def get_presets():
    return {"presets": presets.PRESETS}


# ---------------------------------------------------------------- 硬件
@app.get("/api/hw")
async def hardware():
    return hw.state()


@app.post("/api/hw/refresh")
async def hardware_refresh():
    await run_in_threadpool(hw.refresh)
    return {"ok": True}


# 自检要实测四个编码器，一次几十秒。缓存结果 + 冷却时间，避免被连点或脚本刷爆 CPU
_report_cache: dict[str, Any] = {"at": 0.0, "data": None}


@app.get("/api/hw/report")
async def hardware_report():
    """完整自检报告：设备、vainfo、MFX 运行时、四个编码器实测，一次跑完给页面看。"""
    now = time.time()
    cooldown = config.HW_REPORT_COOLDOWN
    if _report_cache["data"] is not None and now - _report_cache["at"] < cooldown:
        return {**_report_cache["data"], "cached": True,
                "next_allowed_in": round(cooldown - (now - _report_cache["at"]), 1)}
    data = await run_in_threadpool(hwdetect.doctor_report)
    _report_cache["at"] = time.time()
    _report_cache["data"] = data
    return {**data, "cached": False}


# ---------------------------------------------------------------- 浏览 / 扫描
@app.get("/api/browse")
async def browse(path: str = ""):
    target = path or (config.MEDIA_ROOTS[0] if config.MEDIA_ROOTS else "/")
    try:
        # list_dir 里是同步 stat/scandir，别占着事件循环
        return await run_in_threadpool(scanner.list_dir, target)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, f"无法读取目录：{exc}") from exc


@app.post("/api/scan")
async def scan(req: ScanReq):
    try:
        # 扫描会跑几十上百次 ffprobe（每次几十毫秒到几秒），同步执行会把整个事件循环钉住：
        # 期间 /api/jobs、/api/health 全部无响应，前端看着像卡死。
        return await run_in_threadpool(
            scanner.scan,
            req.path, req.recursive, req.preset_id, req.preference,
            req.custom_crf, req.min_size_mb, req.manual,
            req.exclude, req.ignore_history,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"扫描失败：{exc}") from exc


@app.post("/api/estimate")
async def estimate(req: EstimateReq):
    if req.items:
        payload = [i.model_dump() for i in req.items]
    else:
        payload = [{
            "path": p,
            "preset_id": req.preset_id,
            "manual": req.manual,
            "custom_crf": req.custom_crf,
            "tracks": req.tracks.get(p),
        } for p in req.paths]

    # 路径白名单：estimate 只读元数据，但同样是任意文件读取面（能探测宿主机上有哪些文件、
    # 大小、时长），必须和扫描用同一条边界
    allowed = [i for i in payload if i.get("path") and scanner.is_allowed(i["path"])]
    if payload and not allowed:
        raise HTTPException(403, "路径不在允许的媒体根目录内")

    return {"items": await run_in_threadpool(
        scanner.reestimate, allowed, req.preference, req.ignore_history)}


# ---------------------------------------------------------------- 任务
@app.post("/api/jobs")
async def create_jobs(req: JobsReq):
    if not req.items:
        raise HTTPException(400, "没有选中任何文件")
    if len(req.items) > config.MAX_JOBS_PER_REQUEST:
        raise HTTPException(400, f"一次最多提交 {config.MAX_JOBS_PER_REQUEST} 个任务，请分批")

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in req.items:
        if not os.path.isfile(item.path):
            continue
        if not scanner.is_allowed(item.path):
            raise HTTPException(403, f"路径不在允许范围：{item.path}")
        # 同一源文件已经在队列里/正在跑：两条任务会算出同一个输出路径互相覆盖，白占一份 CPU
        if manager.has_active(item.path):
            skipped.append({"path": item.path, "reason": "已有同一文件的任务在进行中"})
            continue
        # 只接受软字幕轨模式；烧录会二次损失画质，已从产品中移除
        mode = item.subtitle_mode if item.subtitle_mode in ("keep", "none") else "keep"
        job = new_job(
            item.path,
            preset_id=item.preset_id or req.preset_id,
            preference=req.preference,
            subtitle_mode=mode,
            subtitles=[s for s in item.subtitles if os.path.exists(s) and scanner.is_allowed(s)],
            audio_mode=item.audio_mode,
            audio_tracks=item.audio_tracks,
            custom_crf=item.custom_crf,
            manual=item.manual or req.manual,
        )
        manager.add(job)
        created.append(job.public(light=True))

    if not created:
        if skipped:
            raise HTTPException(409, skipped[0]["reason"])
        raise HTTPException(400, "选中的文件都不存在了，请重新扫描")
    return {"jobs": created, "skipped": skipped}


@app.get("/api/jobs")
async def list_jobs(rev: int | None = None):
    """任务列表，支持按版本号做增量。

    前端每 1.5 秒轮询一次，可压一个 4K 电影要几小时——绝大多数轮询什么都没变。
    带 ?rev=<上次的版本号> 且确实没变化时，只回几十字节的 changed:false；
    有变化才回全量，并且只带界面用得到的字段（不含 ffmpeg 命令行等大字段）。
    """
    current = manager.rev
    if rev is not None and rev == current:
        return {"rev": current, "changed": False}
    return {"rev": current, "changed": True, "jobs": manager.list_public(light=True)}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    ok = await manager.cancel(job_id)
    if not ok:
        raise HTTPException(404, "任务不存在或已结束")
    return {"ok": True}


@app.post("/api/jobs/pause")
async def pause_jobs(req: PauseReq):
    """暂停一批任务（排队中的标记暂停，运行中的给 ffmpeg 发 SIGSTOP 冻结）。"""
    if not req.job_ids:
        raise HTTPException(400, "没有指定任务")
    return manager.pause(req.job_ids)


@app.post("/api/jobs/resume")
async def resume_jobs(req: PauseReq):
    """恢复一批被暂停的任务（重新入队 / 给 ffmpeg 发 SIGCONT）。"""
    if not req.job_ids:
        raise HTTPException(400, "没有指定任务")
    return manager.resume(req.job_ids)


@app.post("/api/jobs/source")
async def job_source_action(req: SourceActionReq):
    """压缩完成后处理源文件：保留 / 改名归档（加 .old 后缀）/ 删除。"""
    if req.action not in ("keep", "rename", "delete"):
        raise HTTPException(400, "action 只能是 keep / rename / delete")
    if not req.job_ids:
        raise HTTPException(400, "没有指定任务")
    if req.action == "delete" and not req.confirm:
        raise HTTPException(400, "删除源文件必须显式确认（confirm=true）")
    # 这些全是磁盘操作（可能要几秒），放线程池里跑，别卡住事件循环
    return await run_in_threadpool(manager.source_action, req.job_ids, req.action)


@app.post("/api/jobs/rename-output")
async def rename_output(req: RenameOutputReq):
    """把转码结果改回源文件名（去掉 _compressed 之类后缀）。

    只有源文件已经被删除或改名归档（腾出原名）后才允许，否则会覆盖源文件。
    """
    if not req.job_ids:
        raise HTTPException(400, "没有指定任务")
    return await run_in_threadpool(manager.rename_outputs, req.job_ids)


@app.post("/api/jobs/clear")
async def clear_jobs():
    return {"removed": manager.clear_finished()}


# ---------------------------------------------------------------- 转码历史
@app.get("/api/history")
async def list_history(q: str = "", limit: int = 100, offset: int = 0):
    """转码历史，新→旧。带 q 时按源/输出路径过滤。

    首次访问要把 JSON 读进内存，几 MB 的读取也走线程池，别让事件循环等着。
    """
    limit = max(1, min(500, limit))
    offset = max(0, offset)
    return await run_in_threadpool(history.history.listing, q, limit, offset)


@app.post("/api/history/clear")
async def clear_history():
    return {"removed": await run_in_threadpool(history.history.clear)}