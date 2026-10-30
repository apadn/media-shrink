#!/usr/bin/env bash
# 端到端自检：
#   1) 编码分组（H.264 进「需转码」，HEVC 进「已是目标编码」）
#   2) 字幕只做软轨，不烧录画面
#   3) 音轨逐条选择，输出只保留勾选的轨
set -euo pipefail

IMG="${IMG:-xdxy/media-shrink:test}"
WORK=/tmp/ms-e2e
PORT="${PORT:-18420}"
export PORT WORK
NAME=ms-e2e
# 用哪个 python 解析 JSON：PYTHON 环境变量 > python3 > python
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  PY=$(command -v python3 || command -v python || true)
fi
[ -n "$PY" ] || { echo "需要 python3 来解析接口返回的 JSON" >&2; exit 1; }
export PY

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
bad() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

say "准备测试素材"
docker rm -f "$NAME" >/dev/null 2>&1 || true
rm -rf "$WORK"; mkdir -p "$WORK/media" "$WORK/out"
docker run --rm -v "$WORK/media:/work" --entrypoint bash "$IMG" -c '
set -e
cd /work

# 1) H.264 + 单音轨 + 外挂字幕
ffmpeg -y -v error -f lavfi -i testsrc2=size=1280x720:rate=25:duration=8 \
  -f lavfi -i sine=frequency=440:duration=8 \
  -c:v libx264 -preset ultrafast -b:v 6M -c:a aac -b:a 128k /work/movie.mkv

# 2) H.264 无音轨
ffmpeg -y -v error -f lavfi -i testsrc2=size=1920x1080:rate=25:duration=6 \
  -c:v libx264 -preset ultrafast -b:v 12M /work/clip.mp4

# 3) H.264 + 两条音轨（立体声中文 / 5.1 英文），用于验证音轨选择
ffmpeg -y -v error \
  -f lavfi -i testsrc2=size=1280x720:rate=25:duration=6 \
  -f lavfi -i sine=frequency=440:duration=6 \
  -f lavfi -i sine=frequency=880:duration=6 \
  -map 0:v -map 1:a -map 2:a \
  -c:v libx264 -preset ultrafast -b:v 4M \
  -c:a:0 aac -b:a:0 128k -ac:a:0 2 -metadata:s:a:0 language=chi -metadata:s:a:0 title="中文" \
  -c:a:1 ac3 -b:a:1 384k -ac:a:1 6 -metadata:s:a:1 language=eng -metadata:s:a:1 title="English" \
  /work/multi.mkv

# 4) 已经是 HEVC，用于验证「已是目标编码」分组
ffmpeg -y -v error -f lavfi -i testsrc2=size=1280x720:rate=25:duration=5 \
  -c:v libx265 -preset ultrafast -crf 30 -x265-params log-level=error \
  -pix_fmt yuv420p10le /work/already.mkv

printf "1\n00:00:01,000 --> 00:00:04,000\n中文字幕测试 Subtitle OK\n\n2\n00:00:05,000 --> 00:00:07,500\nSecond line\n" > /work/movie.zh.srt
printf "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,WenQuanYi Micro Hei,42,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,30,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,ASS 中文字幕\n" > /work/movie.eng.ass
ls -la /work
'

say "启动服务容器（端口 ${PORT}）"
docker run -d --name "$NAME" -p "$PORT:8000" \
  -v "$WORK/media:/media" -v "$WORK/out:/output" \
  "$IMG" >/dev/null
for i in $(seq 1 40); do
  if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then ok "服务已就绪"; break; fi
  sleep 1
  [ "$i" = 40 ] && { bad "服务启动超时"; docker logs "$NAME" | tail -30; exit 1; }
done

say "等待硬件探测完成"
for i in $(seq 1 60); do
  P=$("$PY" - <<'PYEOF'
import json, os, urllib.request
try:
    d = json.load(urllib.request.urlopen("http://localhost:%s/api/hw" % os.environ["PORT"], timeout=5))
    print("done" if not d.get("probing") else "waiting")
except Exception:
    print("waiting")
PYEOF
)
  [ "$P" = "done" ] && break
  sleep 2
done
curl -s "http://localhost:$PORT/api/hw" > "$WORK/hw.json"
"$PY" - <<'PYEOF'
import json, os
d = json.load(open(os.path.join(os.environ["WORK"], "hw.json")))
print("  ffmpeg:", (d.get("ffmpeg") or "")[:70])
for e in d.get("encoders", []):
    if e["available"]:
        print("  可用: %-14s (%s)" % (e["name"], e["kind"]))
print("  推荐编码器:", d.get("summary"))
dev = d.get("devices", {})
print("  /dev/dri:", dev.get("vaapi_dri"), " 驱动:", dev.get("vaapi_driver") or "无")
PYEOF

say "扫描 /media 并验证编码分组"
curl -s -X POST "http://localhost:$PORT/api/scan" -H 'Content-Type: application/json' \
  -d '{"path":"/media","recursive":true,"preset_id":"hevc-high","preference":"cpu","min_size_mb":0}' \
  > "$WORK/scan.json"
"$PY" - <<'PYEOF'
import json, os, sys

d = json.load(open(os.path.join(os.environ["WORK"], "scan.json")))
if "items" not in d:
    print("  ✗ 扫描失败:", d); sys.exit(1)

items = d["items"]
st = d["stats"]
print("  文件数: %d   读取错误: %d" % (len(items), len(d.get("errors", []))))

for it in items:
    subs = [s["filename"] for s in it.get("subtitles", [])]
    print("  - %-12s %-5s(%s)  %7.2fMB %dx%d  音轨%d  %-5s  字幕=%s" % (
        it["filename"], it["codec"], it["codec_family"], it["size"] / 1e6,
        it["width"], it["height"], len(it.get("audio", [])), it["suggestion"], subs))

convert = [i for i in items if i["codec_family"] == "other"]
already = [i for i in items if i["codec_family"] != "other"]
print("\n  需转码组: %d 个 -> %s" % (len(convert), [i["filename"] for i in convert]))
print("  已是 AV1/HEVC 组: %d 个 -> %s" % (len(already), [i["filename"] for i in already]))

ok = True
if st["convert_count"] != 3:
    print("  ✗ convert_count 期望 3，实际", st["convert_count"]); ok = False
if st["already_count"] != 1:
    print("  ✗ already_count 期望 1，实际", st["already_count"]); ok = False
if [i["filename"] for i in already] != ["already.mkv"]:
    print("  ✗ 已是 HEVC 的文件应只有 already.mkv"); ok = False
if not already or already[0]["suggestion"] != "skip":
    print("  ✗ 已是 HEVC 且低码率的文件应给出 skip 建议"); ok = False

multi = next((i for i in items if i["filename"] == "multi.mkv"), None)
if not multi or len(multi["audio"]) != 2:
    print("  ✗ multi.mkv 应识别出 2 条音轨"); ok = False
else:
    print("  multi.mkv 音轨: %s" % [
        "#%s %s %sch %s" % (a["index"], a["codec"], a["channels"], a["language"]) for a in multi["audio"]])

print("  ✓ 分组与音轨元数据正确" if ok else "  ✗ 分组校验未通过")
sys.exit(0 if ok else 1)
PYEOF

say "任务 1：仅重封装 + 注入外挂字幕（软轨，不烧录）"
"$PY" - <<'PYEOF' > "$WORK/job1.json"
import json
print(json.dumps({"preset_id": "remux", "preference": "cpu", "items": [{
    "path": "/media/movie.mkv", "preset_id": "remux", "subtitle_mode": "keep",
    "subtitles": ["/media/movie.zh.srt", "/media/movie.eng.ass"]}]}))
PYEOF
curl -s -X POST "http://localhost:$PORT/api/jobs" -H 'Content-Type: application/json' \
  -d @"$WORK/job1.json" > /dev/null
sleep 8
curl -s "http://localhost:$PORT/api/jobs" > "$WORK/jobs1.json"
"$PY" - <<'PYEOF'
import json, os
for j in json.load(open(os.path.join(os.environ["WORK"], "jobs1.json")))["jobs"]:
    print("  [%s] %s -> %s" % (j["status"], j["src"].split("/")[-1], j["out"].split("/")[-1]))
    print("        %s" % j["message"])
    if j["status"] == "failed":
        print("        cmd:", j["cmd_preview"][:500])
PYEOF

say "任务 2：HEVC 压缩（CPU libx265）"
"$PY" - <<'PYEOF' > "$WORK/job2.json"
import json
print(json.dumps({"preset_id": "hevc-high", "preference": "cpu", "items": [{
    "path": "/media/clip.mp4", "preset_id": "hevc-high",
    "subtitle_mode": "none", "subtitles": []}]}))
PYEOF
curl -s -X POST "http://localhost:$PORT/api/jobs" -H 'Content-Type: application/json' \
  -d @"$WORK/job2.json" > /dev/null

say "任务 3：只保留第 1 条音轨（丢弃 5.1 英文轨）"
"$PY" - <<'PYEOF' > "$WORK/job3.json"
import json
print(json.dumps({"preset_id": "remux", "preference": "cpu", "items": [{
    "path": "/media/multi.mkv", "preset_id": "remux", "subtitle_mode": "keep",
    "subtitles": [], "audio_tracks": [1]}]}))
PYEOF
curl -s -X POST "http://localhost:$PORT/api/jobs" -H 'Content-Type: application/json' \
  -d @"$WORK/job3.json" > /dev/null

for i in $(seq 1 120); do
  curl -s "http://localhost:$PORT/api/jobs" > "$WORK/jobs.json"
  ST=$("$PY" - <<'PYEOF'
import json, os
js = json.load(open(os.path.join(os.environ["WORK"], "jobs.json")))["jobs"]
alive = [j for j in js if j["status"] in ("queued", "running")]
print("running:%d" % len(alive) if alive else "done")
PYEOF
)
  case "$ST" in done) break ;; esac
  sleep 3
done

say "任务结果"
"$PY" - <<'PYEOF'
import json, os
for j in json.load(open(os.path.join(os.environ["WORK"], "jobs.json")))["jobs"]:
    print("  [%s] %-12s %s" % (j["status"], j["src"].split("/")[-1], j["message"]))
    if j["status"] == "failed":
        print("     cmd:", j["cmd_preview"][:600])
PYEOF

say "校验输出文件"
docker run --rm -v "$WORK/media:/m" -v "$WORK/out:/o" --entrypoint bash "$IMG" -c '
for f in /m/*_compressed.* /o/*; do
  [ -f "$f" ] || continue
  echo "--- $f ($(du -h "$f" | cut -f1))"
  ffprobe -v error -show_entries stream=index,codec_type,codec_name,width,height,channels:stream_tags=language,title \
    -of compact=p=0:nk=0 "$f" | sed "s/^/    /"
done
'

"$PY" - <<'PYEOF'
import json, os, subprocess, sys

work = os.environ["WORK"]
img = os.environ.get("IMG", "xdxy/media-shrink:test")

def probe(stem):
    """按 stem 找输出文件（默认 mkv；仍兼容 mp4/webm，方便改配置后回归）。"""
    for ext in ("mkv", "mp4", "webm"):
        name = stem + "_compressed." + ext
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", "%s/media:/m" % work, "--entrypoint", "ffprobe", img,
             "-v", "error", "-print_format", "json", "-show_streams", "/m/" + name],
            capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout).get("streams", [])
    return []

ok = True

# 重封装：视频必须是 copy（未被重编码/烧录），字幕轨 2 条
s = probe("movie")
vids = [x for x in s if x["codec_type"] == "video"]
subs = [x for x in s if x["codec_type"] == "subtitle"]
if len(vids) != 1 or vids[0]["codec_name"] != "h264":
    print("  ✗ 重封装输出的视频应保持 h264（说明没有烧录字幕）:", [v.get("codec_name") for v in vids]); ok = False
else:
    print("  ✓ 视频流保持 h264 / %dx%d（未烧录字幕）" % (vids[0]["width"], vids[0]["height"]))
if len(subs) != 2:
    print("  ✗ 期望 2 条软字幕轨，实际", len(subs)); ok = False
else:
    langs = [(t.get("tags") or {}).get("language", "") for t in subs]
    print("  ✓ 软字幕轨 2 条，语言标签:", langs)
    if "chi" not in langs:
        print("  ✗ 中文轨语言标签缺失"); ok = False

# 音轨裁剪：multi.mkv 客户端只选了 1 条
s = probe("multi")
auds = [x for x in s if x["codec_type"] == "audio"]
if len(auds) != 1:
    print("  ✗ 期望只保留 1 条音轨，实际", len(auds)); ok = False
else:
    a = auds[0]
    print("  ✓ 只保留 1 条音轨: %s %sch lang=%s" % (
        a["codec_name"], a.get("channels"), (a.get("tags") or {}).get("language")))
    if (a.get("tags") or {}).get("language") != "chi":
        print("  ✗ 保留的应该是中文轨（index 1）"); ok = False

# HEVC 压缩：编码变了、分辨率保持
s = probe("clip")
vids = [x for x in s if x["codec_type"] == "video"]
if not vids or vids[0]["codec_name"] != "hevc":
    print("  ✗ clip 应被压成 hevc:", [v.get("codec_name") for v in vids]); ok = False
elif (vids[0]["width"], vids[0]["height"]) != (1920, 1080):
    print("  ✗ 分辨率应保持 1920x1080，实际", vids[0]["width"], vids[0]["height"]); ok = False
else:
    print("  ✓ clip 压成 hevc，分辨率保持 1920x1080")

# 容器：默认全部输出 mkv（源文件是 mkv / mp4 / mkv 三种）
import glob
outs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(work, "media", "*_compressed.*")))
print("  输出文件:", outs)
not_mkv = [o for o in outs if not o.endswith(".mkv")]
if not_mkv:
    print("  ✗ 默认应全部输出 mkv，实际:", not_mkv); ok = False
elif len(outs) != 3:
    print("  ✗ 期望 3 个输出文件，实际", len(outs)); ok = False
else:
    print("  ✓ 3 个输出全部为 MKV（mp4 源也转成 mkv）")

print("\n" + ("全部校验通过" if ok else "存在未通过项"))
sys.exit(0 if ok else 1)
PYEOF

say "源文件处理：改名归档 / 删除 / 保留"
"$PY" - <<'PYEOF'
import json, os, sys, urllib.error, urllib.request

port = os.environ["PORT"]
media = os.path.join(os.environ["WORK"], "media")
base = "http://localhost:%s" % port
ok = True

def post(path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30)), 200
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}"), e.code

jobs = {j["src"].split("/")[-1]: j["id"] for j in
        json.load(urllib.request.urlopen(base + "/api/jobs", timeout=10))["jobs"]}

# 1) 删除必须显式确认，否则应被拒绝
r, code = post("/api/jobs/source", {"job_ids": [jobs["clip.mp4"]], "action": "delete"})
if code != 400:
    print("  ✗ 未确认的删除应被拒绝（400），实际", code); ok = False
else:
    print("  ✓ 未确认的删除被拒绝:", r.get("detail", "")[:40])
if not os.path.exists(os.path.join(media, "clip.mp4")):
    print("  ✗ 被拒绝后源文件不应消失"); ok = False

# 2) 改名归档：movie.mkv -> movie.old.mkv
r, code = post("/api/jobs/source", {"job_ids": [jobs["movie.mkv"]], "action": "rename"})
archived = os.path.join(media, "movie.old.mkv")
if code != 200 or not r.get("ok"):
    print("  ✗ 改名失败:", r); ok = False
elif not os.path.exists(archived):
    print("  ✗ 期望生成 movie.old.mkv"); ok = False
elif os.path.exists(os.path.join(media, "movie.mkv")):
    print("  ✗ 原名 movie.mkv 应该已经不在了"); ok = False
else:
    print("  ✓ 源文件已改名归档为 movie.old.mkv")

# 3) 同一个任务不能重复处理
r, code = post("/api/jobs/source", {"job_ids": [jobs["movie.mkv"]], "action": "rename"})
if code != 200 or r.get("ok") or not r.get("results"):
    print("  ✗ 重复处理应返回 ok=false:", r); ok = False
else:
    print("  ✓ 重复处理被拒绝:", r["results"][0]["message"])

# 4) 删除（带确认）：clip.mp4 消失，输出仍在
r, code = post("/api/jobs/source", {"job_ids": [jobs["clip.mp4"]], "action": "delete", "confirm": True})
if code != 200 or not r.get("ok"):
    print("  ✗ 确认后删除失败:", r); ok = False
elif os.path.exists(os.path.join(media, "clip.mp4")):
    print("  ✗ clip.mp4 应已删除"); ok = False
elif not os.path.exists(os.path.join(media, "clip_compressed.mkv")):
    print("  ✗ 压缩结果不应被误删"); ok = False
else:
    print("  ✓ 源文件已删除，压缩结果保留")

# 5) 保留：multi.mkv 原样不动
r, code = post("/api/jobs/source", {"job_ids": [jobs["multi.mkv"]], "action": "keep"})
if code != 200 or not r.get("ok") or not os.path.exists(os.path.join(media, "multi.mkv")):
    print("  ✗ 保留操作异常:", r); ok = False
else:
    print("  ✓ 选择保留，源文件未被改动")

print("  源文件处理校验通过" if ok else "  源文件处理存在未通过项")
sys.exit(0 if ok else 1)
PYEOF

say "手动配置：自定义编码 / 目标码率 / 位深"
"$PY" - <<'PYEOF'
import json, os, sys, time, urllib.request

port = os.environ["PORT"]
base = "http://localhost:%s" % port
ok = True

def post(path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))

# 1) 扫描时带手动配置：目标编码应变成 AV1，源编码分组不受影响
d = post("/api/scan", {"path": "/media", "recursive": True, "preset_id": "custom",
                       "preference": "cpu", "min_size_mb": 0,
                       "manual": {"codec": "av1", "mode": "crf", "crf": 30, "speed": "fast"}})
label = d.get("preset_label") or ""
print("  扫描预设标签:", label)
if "AV1" not in label or "CRF 30" not in label:
    print("  ✗ 手动配置标签未生效"); ok = False
mv = next((i for i in d["items"] if i["filename"] == "movie.old.mkv"), None)
if not mv:
    print("  ✗ 找不到 movie.old.mkv"); ok = False
elif mv.get("encoder") != "libsvtav1":
    print("  ✗ 手动选 AV1 后编码器应为 libsvtav1，实际", mv.get("encoder")); ok = False
elif mv["codec_family"] != "other":
    print("  ✗ 源编码分组不应受目标编码影响"); ok = False
else:
    print("  ✓ 扫描按手动配置选到 libsvtav1，预估 %.2fMB" % (mv["estimate_bytes"] / 1e6))

# 2) 预估：码率模式必须是"码率 × 时长"，同一批里还能混普通预设
r = post("/api/estimate", {"preference": "cpu", "items": [
    {"path": "/media/movie.old.mkv", "preset_id": "custom",
     "manual": {"codec": "hevc", "mode": "bitrate", "bitrate": 900, "audio": "copy"}},
    {"path": "/media/multi.mkv", "preset_id": "hevc-high"},
]})
est = r["items"]["/media/movie.old.mkv"]
want = (900_000 + 128_000) * 8.0 / 8   # movie 片段 8 秒
if abs(est["estimate_bytes"] - want) / want > 0.06:
    print("  ✗ 码率模式预估偏差过大:", est["estimate_bytes"], "期望约", int(want)); ok = False
else:
    print("  ✓ 目标码率预估 %.2fMB（由码率与时长算出）" % (est["estimate_bytes"] / 1e6))
if r["items"]["/media/multi.mkv"]["estimate_bytes"] <= 0:
    print("  ✗ 混用普通预设的那一项没有结果"); ok = False

# 3) 真跑一次：HEVC 900kbps + 强制 10bit
post("/api/jobs", {"preference": "cpu", "items": [{
    "path": "/media/movie.old.mkv", "preset_id": "custom",
    "manual": {"codec": "hevc", "mode": "bitrate", "bitrate": 900,
               "speed": "fast", "bit_depth": 10, "audio": "copy"},
    "subtitle_mode": "none", "subtitles": []}]})

j = None
for _ in range(60):
    js = json.load(urllib.request.urlopen(base + "/api/jobs", timeout=10))["jobs"]
    j = next((x for x in js if x["src"].endswith("movie.old.mkv")), None)
    if j and j["status"] in ("done", "failed", "canceled"):
        break
    time.sleep(2)
print("  任务:", j and j["status"], "|", j and j["message"])
if not j or j["status"] != "done":
    print("  ✗ 手动配置任务失败:", j and j.get("cmd_preview")); ok = False

sys.exit(0 if ok else 1)
PYEOF

say "校验手动配置的输出（HEVC / 720p / 10bit / 码率）"
docker run --rm -v "$WORK/media:/m" --entrypoint bash "$IMG" -c '
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,pix_fmt,bit_rate \
  -of default=nw=1 /m/movie.old_compressed.mkv
' | tee /tmp/ms-manual.txt

"$PY" - <<'PYEOF'
import os, sys
vals = {}
for line in open("/tmp/ms-manual.txt"):
    if "=" in line:
        k, _, v = line.strip().partition("=")
        vals[k] = v
print("  ", vals)
ok = True
if vals.get("codec_name") != "hevc":
    print("  ✗ 编码应为 hevc"); ok = False
if vals.get("height") != "720":
    print("  ✗ 高度应保持 720"); ok = False
if vals.get("pix_fmt") != "yuv420p10le":
    print("  ✗ 位深应为 10bit（yuv420p10le）"); ok = False

# mkv 的流级 bit_rate 常为 N/A，改用文件体积 / 时长折算
out = os.path.join(os.environ["WORK"], "media", "movie.old_compressed.mkv")
size = os.path.getsize(out) if os.path.exists(out) else 0
br = size * 8 / 8.0        # 8 秒素材
print("   实测平均码率约 %d kbps（文件 %.1fKB）" % (br / 1000, size / 1024))
if not (600_000 <= br <= 1_400_000):
    print("  ✗ 实际码率应在 900kbps 附近"); ok = False
print("  手动配置输出校验通过" if ok else "  手动配置输出校验未通过")
sys.exit(0 if ok else 1)
PYEOF

say "转码历史：记录已落盘，重扫能认出已转过"

curl -s "http://localhost:$PORT/api/history" > "$WORK/history.json"
curl -s -X POST "http://localhost:$PORT/api/scan" -H 'Content-Type: application/json' \
  -d '{"path":"/media","min_size_mb":0,"preset_id":"hevc-fast"}' > "$WORK/scan2.json"

"$PY" - <<'PYEOF'
import json, os, sys
work = os.environ["WORK"]
h = json.load(open(os.path.join(work, "history.json")))
s = json.load(open(os.path.join(work, "scan2.json")))
ok = True
print("   历史条数:", h["total"], "| 累计省:", h["stats"]["bytes_saved"], "字节")
if h["total"] < 1:
    print("  ✗ 转码完成后历史应该是非空的"); ok = False
hit = {i["filename"]: i for i in s["items"] if i.get("history")}
print("   扫描命中历史:", {k: v["history"]["match"] for k, v in hit.items()})
if not hit:
    print("  ✗ 重扫没有认出任何已转过的文件"); ok = False
src_hits = [k for k, v in hit.items() if v["history"]["match"] == "src"]
out_hits = [k for k, v in hit.items() if v["history"]["match"] == "out"]
if not src_hits:
    print("  ✗ 没有源文件命中（match=src）"); ok = False
if not out_hits:
    print("  ✗ 没有输出文件命中（match=out）"); ok = False
bad = [k for k, v in hit.items() if v["suggestion"] != "skip"]
if bad:
    print("  ✗ 命中历史的文件建议应为 skip:", bad); ok = False

import urllib.request
body = json.dumps({"path": "/media", "min_size_mb": 0, "preset_id": "hevc-fast",
                   "ignore_history": True}).encode()
rq = urllib.request.Request("http://localhost:%s/api/scan" % os.environ["PORT"], data=body,
                            headers={"Content-Type": "application/json"})
s2 = json.loads(urllib.request.urlopen(rq, timeout=300).read())
if s2["stats"].get("history_count"):
    print("  ✗ ignore_history 下不该有命中，实际:", s2["stats"]["history_count"]); ok = False
print("  转码历史校验通过" if ok else "  转码历史校验未通过")
sys.exit(0 if ok else 1)
PYEOF

say "自检结束"
