"""硬件能力探测。

不做"grep ffmpeg -encoders"这种表面检测，而是对每个候选编码器实际跑一次
0.3 秒的测试编码 —— 只有真跑通才算可用。这样不会出现"检测支持但一压就崩"。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from . import config

# 候选编码器：顺序即同一 kind 内的优先顺序
CANDIDATES: list[dict[str, str]] = [
    # AV1
    {"name": "av1_nvenc", "codec": "av1", "kind": "nvidia"},
    {"name": "av1_qsv", "codec": "av1", "kind": "intel"},
    {"name": "av1_amf", "codec": "av1", "kind": "amd"},
    {"name": "av1_vaapi", "codec": "av1", "kind": "vaapi"},
    {"name": "libsvtav1", "codec": "av1", "kind": "cpu"},
    {"name": "libaom-av1", "codec": "av1", "kind": "cpu"},
    # HEVC
    {"name": "hevc_nvenc", "codec": "hevc", "kind": "nvidia"},
    {"name": "hevc_qsv", "codec": "hevc", "kind": "intel"},
    {"name": "hevc_amf", "codec": "hevc", "kind": "amd"},
    {"name": "hevc_vaapi", "codec": "hevc", "kind": "vaapi"},
    {"name": "libx265", "codec": "hevc", "kind": "cpu"},
    # H.264
    {"name": "h264_nvenc", "codec": "h264", "kind": "nvidia"},
    {"name": "h264_qsv", "codec": "h264", "kind": "intel"},
    {"name": "h264_amf", "codec": "h264", "kind": "amd"},
    {"name": "h264_vaapi", "codec": "h264", "kind": "vaapi"},
    {"name": "libx264", "codec": "h264", "kind": "cpu"},
]

PREF_ORDER = {
    "auto": ["nvidia", "intel", "amd", "vaapi", "cpu"],
    "cpu": ["cpu"],
    "intel": ["intel", "vaapi", "cpu"],
    "nvidia": ["nvidia", "cpu"],
    "amd": ["amd", "vaapi", "cpu"],
}

KIND_LABEL = {
    "cpu": "CPU 软件编码",
    "intel": "Intel QSV 核显",
    "nvidia": "NVIDIA NVENC",
    "amd": "AMD AMF",
    "vaapi": "VAAPI 通用硬件",
}

# 常见报错 → 人话解释。命中第一条即用，没命中就只显示原始 stderr。
ERROR_HINTS: list[tuple[str, str]] = [
    ("Error initializing an internal MFX session",
     "QSV 会话建不起来。8~10 代核显（HD 630、UHD 630、HD 530 这类）的 QSV 必须靠旧版 "
     "Media SDK 运行时 libmfxhw64（Debian 包 libmfx1）；镜像里缺它就一定报这个。"
     "1.0.5 起已内置，若仍报错说明宿主机 i915 与该运行时版本不匹配——改用 VAAPI 即可，同样是核显编码"),
    ("Error loading plugins",
     "oneVPL 加载不到 GPU 运行时插件：11 代以上需要 libmfx-gen1.2，8~10 代需要 libmfx1。"
     "镜像 1.0.5 起两者都装；若仍失败，编码器选「自动」会退回 VAAPI"),
    ("Device creation failed",
     "创建硬件设备失败：确认容器的 --device=/dev/dri:/dev/dri 生效，容器内 ls /dev/dri 能看到 renderD128"),
    ("Cannot load libmfx",
     "缺少 MFX/VPL 运行库：需要 libmfx1（旧版 MSDK）+ libvpl2，1.0.5 起镜像已内置"),
    ("No usable encoding entrypoint",
     "这块 GPU 没有该编码器的硬件编码入口：老核显不支持 HEVC/AV1 编码，或该 profile（如 10bit）不支持"),
    ("InitializeEncoder failed",
     "编码器初始化失败：常见于驱动不支持所选 profile，例如 10bit/HDR 或 4:4:4"),
    ("Function not implemented",
     "驱动或内核不支持这个操作，通常是 GPU 代际太老"),
    ("Permission denied",
     "没有访问 /dev/dri 的权限：容器需要 --device=/dev/dri:/dev/dri（必要时 --group-add video）"),
    ("No such file or directory",
     "找不到设备文件：/dev/dri 没映射进容器"),
]


def mfx_runtimes() -> dict[str, str]:
    """镜像里装了哪些 MFX/oneVPL 运行时。

    legacy = 旧版 Media SDK（libmfxhw64.so.1.35），8~10 代核显的 QSV 只能走它
    vpl    = oneVPL GPU 运行时（libmfx-gen.so.1.2），11 代及更新平台走它
    """
    out: dict[str, str] = {"legacy": "", "vpl": "", "dispatcher": ""}
    for path in glob.glob("/usr/lib/*/libmfx*"):
        name = os.path.basename(path)
        if name.startswith("libmfxhw64"):
            out["legacy"] = out["legacy"] or path
        elif name.startswith("libmfx-gen"):
            out["vpl"] = out["vpl"] or path
        elif name.startswith("libmfx.so"):
            out["dispatcher"] = out["dispatcher"] or path
    return out


def _explain(err: str) -> str:
    for needle, hint in ERROR_HINTS:
        if needle.lower() in err.lower():
            return hint
    return ""


def _test_args(enc: dict[str, str]) -> list[str]:
    name, codec, kind = enc["name"], enc["codec"], enc["kind"]
    args: list[str] = []
    if kind == "vaapi":
        args += ["-init_hw_device", f"vaapi=va:{config.VAAPI_DEVICE}", "-filter_hw_device", "va"]
    args += ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=0.3"]
    if kind == "vaapi":
        args += ["-vf", "format=nv12,hwupload"]
    if name == "libsvtav1":
        args += ["-c:v", name, "-preset", "12", "-crf", "40"]
    elif name == "libaom-av1":
        args += ["-c:v", name, "-cpu-used", "8", "-crf", "40", "-b:v", "0"]
    elif name == "libx265":
        args += ["-c:v", name, "-preset", "ultrafast", "-crf", "34"]
    elif name == "libx264":
        args += ["-c:v", name, "-preset", "ultrafast", "-crf", "34"]
    elif kind == "nvidia":
        args += ["-c:v", name, "-preset", "p5", "-rc", "vbr", "-cq", "34", "-b:v", "0"]
    elif kind == "intel":
        args += ["-c:v", name, "-global_quality", "34"]
    elif kind == "vaapi":
        args += ["-c:v", name, "-qp", "34"]
    elif kind == "amd":
        args += ["-c:v", name, "-qp_i", "34", "-qp_p", "34"]
    args += ["-f", "null", "-"]
    return args


def _listed_encoders() -> set[str]:
    try:
        cp = subprocess.run([config.FFMPEG, "-hide_banner", "-encoders"],
                            capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def _probe_vaapi() -> dict[str, Any]:
    drivers = sorted(glob.glob("/dev/dri/renderD*"))
    info: dict[str, Any] = {"dri": drivers, "driver": "", "profiles": [], "nodes": []}
    for p in drivers:
        try:
            st = os.stat(p)
        except OSError:
            continue
        info["nodes"].append({"path": p, "mode": oct(st.st_mode & 0o777)[2:], "gid": st.st_gid})
    if not drivers:
        return info
    exe = shutil.which("vainfo")
    if not exe:
        return info
    # 容器里没有 X11，先指定 DRM 显示；最后再退回不带参数的默认探测
    attempts: list[list[str]] = []
    for node in drivers:
        attempts.append(["--display", "drm", "--device", node])
    attempts.append(["--display", "drm", "--device", config.VAAPI_DEVICE])
    attempts.append([])
    seen: set[tuple[str, ...]] = set()
    for extra in attempts:
        key = tuple(extra)
        if key in seen:
            continue
        seen.add(key)
        try:
            cp = subprocess.run([exe, *extra], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            continue
        if cp.returncode == 0:
            for line in cp.stdout.splitlines():
                if "Driver version" in line:
                    info["driver"] = line.split("Driver version:", 1)[-1].strip()
                m = re.match(r"\s*(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)", line)
                if m and m.group(2) == "VAEntrypointEncSlice":
                    info["profiles"].append(m.group(1))
            return info
        # vainfo 失败也留下最后一行，页面能直接看到原因
        err = [l for l in (cp.stderr or cp.stdout or "").strip().splitlines() if l.strip()]
        if err and not info["driver"]:
            info["error"] = " / ".join(err[-2:])[:200]
    return info



def _probe_nvidia() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False, "gpus": []}
    exe = shutil.which("nvidia-smi")
    if not exe:
        return info
    try:
        cp = subprocess.run([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return info
    if cp.returncode == 0:
        info["available"] = True
        info["gpus"] = [l.strip() for l in cp.stdout.splitlines() if l.strip()]
    return info


class HardwareProbe:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probing = False
        self._state: dict[str, Any] = {
            "probing": False,
            "updated_at": 0,
            "devices": {},
            "encoders": [],
            "ffmpeg": "",
            "error": "",
        }

    # ---------------------------------------------------------------- 对外
    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def available(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._state.get("encoders", []) if e.get("available")]

    def pick(self, codec: str, preference: str = "auto") -> dict[str, Any] | None:
        """按偏好挑选该 codec 下最合适的可用编码器。"""
        order = PREF_ORDER.get(preference, PREF_ORDER["auto"])
        with self._lock:
            encs = [dict(e) for e in self._state.get("encoders", [])]
        for kind in order:
            for e in encs:
                if e["codec"] == codec and e["kind"] == kind and e["available"]:
                    return e
        # 兜底：任意可用
        for e in encs:
            if e["codec"] == codec and e["available"]:
                return e
        return None

    def start(self) -> None:
        t = threading.Thread(target=self._run, name="hwprobe", daemon=True)
        t.start()

    def refresh(self) -> None:
        self.start()

    # ---------------------------------------------------------------- 内部
    def _run(self) -> None:
        with self._lock:
            if self._probing:
                return
            self._probing = True
            self._state["probing"] = True
        try:
            result = self._probe_all()
            with self._lock:
                self._state.update(result)
        except Exception as exc:  # noqa: BLE001 - 探测失败不应拖垮服务
            with self._lock:
                self._state["error"] = str(exc)[:300]
        finally:
            with self._lock:
                self._probing = False
                self._state["probing"] = False
                self._state["updated_at"] = int(time.time())

    def _probe_all(self) -> dict[str, Any]:
        version = ""
        try:
            cp = subprocess.run([config.FFMPEG, "-hide_banner", "-version"],
                                capture_output=True, text=True, timeout=30)
            version = (cp.stdout or cp.stderr or "").splitlines()[0] if (cp.stdout or cp.stderr) else ""
        except Exception:  # noqa: BLE001
            version = "ffmpeg 不可用"

        listed = _listed_encoders()
        vaapi = _probe_vaapi()
        nvidia = _probe_nvidia()
        mfx = mfx_runtimes()

        have_hw_device = bool(vaapi.get("dri"))
        encoders: list[dict[str, Any]] = []

        for enc in CANDIDATES:
            item = {
                "name": enc["name"],
                "codec": enc["codec"],
                "kind": enc["kind"],
                "kind_label": KIND_LABEL.get(enc["kind"], enc["kind"]),
                "available": False,
                "message": "",
                "hint": "",
            }
            if enc["name"] not in listed:
                item["message"] = "当前 ffmpeg 未编译该编码器"
                encoders.append(item)
                continue
            if enc["kind"] in ("intel", "vaapi") and not have_hw_device:
                item["message"] = "未发现 /dev/dri 设备（容器未挂载 GPU）"
                encoders.append(item)
                continue
            if enc["kind"] == "amd" and not have_hw_device:
                item["message"] = "未发现 /dev/dri 设备"
                encoders.append(item)
                continue
            if enc["kind"] == "nvidia" and not nvidia.get("available"):
                item["message"] = "未检测到 NVIDIA 驱动（需 --gpus all）"
                encoders.append(item)
                continue
            # 实测
            cmd = [config.FFMPEG, "-hide_banner", "-v", "error", "-y", *_test_args(enc)]
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            except subprocess.TimeoutExpired:
                item["message"] = "测试编码超时"
                encoders.append(item)
                continue
            if cp.returncode == 0:
                item["available"] = True
                item["message"] = "实测通过"
            else:
                # 只留最后一行经常丢掉关键信息，留三段
                lines = [l for l in (cp.stderr or "").strip().splitlines() if l.strip()]
                item["message"] = " / ".join(lines[-3:])[:400] if lines else "测试编码失败"
                item["hint"] = _explain(item["message"])
                # 8~10 代核显的 QSV 完全依赖旧版 MSDK 运行时，缺了就先说明白
                if enc["kind"] == "intel" and not mfx.get("legacy"):
                    item["hint"] = ("镜像内缺少旧版 Media SDK 运行时（libmfxhw64，Debian 包 libmfx1），"
                                    "8~10 代核显的 QSV 必然失败。升级到 1.0.5 以上镜像，"
                                    "或临时改用 VAAPI（编码器选「自动」即可）。")
                # 原样给出复现命令，方便在容器里自己跑一遍
                item["cmd"] = " ".join(cmd[1:])
            encoders.append(item)

        summary = {}
        for codec in ("av1", "hevc", "h264"):
            best = None
            for e in encoders:
                if e["codec"] == codec and e["available"]:
                    best = e
                    break
            summary[codec] = best["name"] if best else None

        hw_available = [e["name"] for e in encoders if e["available"] and e["kind"] != "cpu"]

        return {
            "ffmpeg": version,
            "devices": {
                "vaapi_dri": vaapi.get("dri", []),
                "vaapi_driver": vaapi.get("driver", ""),
                "vaapi_profiles": vaapi.get("profiles", []),
                "vaapi_error": vaapi.get("error", ""),
                "dri_nodes": vaapi.get("nodes", []),
                "nvidia": nvidia,
                "dri_mounted": have_hw_device,
                "mfx": mfx,
            },
            "encoders": encoders,
            "summary": summary,
            "hw_available": hw_available,
        }


def doctor_report(timeout: int = 240) -> dict[str, Any]:
    """跑一遍 tools/hw-doctor.sh，把完整诊断输出交回给页面。

    脚本随镜像装在 /usr/local/bin/hw-doctor，也可以在容器里直接执行。
    """
    exe = shutil.which("hw-doctor")
    if not exe:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cand in ("/usr/local/bin/hw-doctor", os.path.join(here, "tools", "hw-doctor.sh")):
            if os.path.exists(cand):
                exe = cand
                break
    if not exe:
        return {"ok": False, "text": "镜像内没有 hw-doctor 脚本（1.0.5 起内置，可直接 docker exec <容器> hw-doctor）"}
    try:
        cp = subprocess.run(["sh", exe], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": f"自检超时（>{timeout}s），请在容器里手动执行 hw-doctor"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": f"自检执行失败：{exc}"}
    text = (cp.stdout or "").rstrip()
    if cp.stderr and cp.stderr.strip():
        text += "\n\n--- stderr ---\n" + cp.stderr.strip()
    return {"ok": True, "text": text[:20000]}


probe = HardwareProbe()
