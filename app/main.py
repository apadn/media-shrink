"""FastAPI 入口：REST API + 静态前端。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, hwdetect, presets, scanner
from .hwdetect import probe as hw
from .transcoder import manager, new_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    hw.start()
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)

if os.path.isdir(config.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


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


# ---------------------------------------------------------------- 基础
@app.get("/")
async def index():
    path = os.path.join(config.STATIC_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "前端文件缺失"}, status_code=500)
    return FileResponse(path)


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
    hw.refresh()
    return {"ok": True}


@app.get("/api/hw/report")
def hardware_report():
    """完整自检报告：设备、vainfo、MFX 运行时、四个编码器实测，一次跑完给页面看。"""
    return hwdetect.doctor_report()


# ---------------------------------------------------------------- 浏览 / 扫描
@app.get("/api/browse")
async def browse(path: str = ""):
    target = path or (config.MEDIA_ROOTS[0] if config.MEDIA_ROOTS else "/")
    try:
        return scanner.list_dir(target)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, f"无法读取目录：{exc}") from exc


@app.post("/api/scan")
async def scan(req: ScanReq):
    try:
        return scanner.scan(
            req.path, req.recursive, req.preset_id, req.preference,
            req.custom_crf, req.min_size_mb, req.manual,
            exclude=req.exclude,
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
    return {"items": scanner.reestimate(payload, req.preference)}


# ---------------------------------------------------------------- 任务
@app.post("/api/jobs")
async def create_jobs(req: JobsReq):
    if not req.items:
        raise HTTPException(400, "没有选中任何文件")

    created: list[dict[str, Any]] = []
    for item in req.items:
        if not os.path.isfile(item.path):
            continue
        if not scanner.is_allowed(item.path):
            raise HTTPException(403, f"路径不在允许范围：{item.path}")
        # 只接受软字幕轨模式；烧录会二次损失画质，已从产品中移除
        mode = item.subtitle_mode if item.subtitle_mode in ("keep", "none") else "keep"
        job = new_job(
            item.path,
            preset_id=item.preset_id or req.preset_id,
            preference=req.preference,
            subtitle_mode=mode,
            subtitles=[s for s in item.subtitles if os.path.exists(s)],
            audio_mode=item.audio_mode,
            audio_tracks=item.audio_tracks,
            custom_crf=item.custom_crf,
            manual=item.manual or req.manual,
        )
        manager.add(job)
        created.append(job.public())

    if not created:
        raise HTTPException(400, "选中的文件都不存在了，请重新扫描")
    return {"jobs": created}


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": manager.list_public()}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    ok = await manager.cancel(job_id)
    if not ok:
        raise HTTPException(404, "任务不存在或已结束")
    return {"ok": True}


@app.post("/api/jobs/source")
async def job_source_action(req: SourceActionReq):
    """压缩完成后处理源文件：保留 / 改名归档（加 .old 后缀）/ 删除。"""
    if req.action not in ("keep", "rename", "delete"):
        raise HTTPException(400, "action 只能是 keep / rename / delete")
    if not req.job_ids:
        raise HTTPException(400, "没有指定任务")
    if req.action == "delete" and not req.confirm:
        raise HTTPException(400, "删除源文件必须显式确认（confirm=true）")
    return manager.source_action(req.job_ids, req.action)


@app.post("/api/jobs/clear")
async def clear_jobs():
    return {"removed": manager.clear_finished()}