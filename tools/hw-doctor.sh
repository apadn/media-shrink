#!/bin/sh
# MediaShrink 硬件自检 —— 一条命令看清核显为什么不转码。
# 用法（宿主机上执行）：
#   docker exec -it <容器名> hw-doctor
# 想存成文件发给别人：
#   docker exec <容器名> hw-doctor > doctor.log 2>&1
set -u

FF="${FFMPEG_BIN:-ffmpeg}"
DEV="${VAAPI_DEVICE:-/dev/dri/renderD128}"
SRC="testsrc2=size=1280x720:rate=30:duration=0.3"

hr() { printf '\n===== %s =====\n' "$1"; }

# $1=标签，其余=ffmpeg 参数（自动补 -f null -）
probe() {
  label="$1"; shift
  printf -- '--- %s\n' "$label"
  out=$("$FF" -hide_banner -v error -y "$@" -f null - 2>&1)
  if [ $? -eq 0 ]; then
    echo "    结果: 通过"
  else
    echo "    结果: 失败"
    printf '%s\n' "$out" | tail -n 4 | sed 's/^/    /'
  fi
}

hr "1. 系统"
uname -srm 2>/dev/null || true
grep -m1 'model name' /proc/cpuinfo 2>/dev/null | sed 's/.*: //' | sed 's/^/CPU: /'
printf '当前用户: %s (uid=%s)\n' "$(id -un 2>/dev/null)" "$(id -u 2>/dev/null)"

hr "2. GPU 设备节点"
ls -l /dev/dri/ 2>/dev/null || echo "!! 容器内没有 /dev/dri —— GPU 设备没映射进来"
printf '目标节点 %s: %s\n' "$DEV" "$(test -e "$DEV" && echo 存在 || echo 不存在)"

hr "3. VAAPI 驱动（vainfo）"
if command -v vainfo >/dev/null 2>&1; then
  vainfo --display drm --device "$DEV" 2>&1 | head -n 24
else
  echo "!! 镜像内没有 vainfo"
fi

hr "4. MFX / oneVPL 运行时"
for f in libmfx.so.1 libmfxhw64.so.1 libmfx-gen.so.1.2 libvpl.so.2; do
  hit=$(ls /usr/lib/*/"$f"* 2>/dev/null | head -n 1)
  if [ -n "$hit" ]; then printf '  %-20s %s\n' "$f" "$hit"; else printf '  %-20s 缺失\n' "$f"; fi
done
echo "  libmfxhw64 = 旧版 Media SDK 运行时，8~10 代核显的 QSV 靠它"
echo "  libmfx-gen = oneVPL GPU 运行时，11 代及更新平台靠它"

hr "5. ffmpeg"
"$FF" -hide_banner -version 2>&1 | head -n 1
"$FF" -hide_banner -buildconf 2>&1 | grep -E -- "--enable-(libvpl|libmfx|vaapi|nvenc)" || true

hr "6. 实测编码（每个 0.3 秒测试片）"
probe "hevc_qsv" -f lavfi -i "$SRC" -c:v hevc_qsv -global_quality 34
probe "h264_qsv" -f lavfi -i "$SRC" -c:v h264_qsv -global_quality 34
probe "hevc_vaapi" -init_hw_device "vaapi=va:$DEV" -filter_hw_device va \
  -f lavfi -i "$SRC" -vf format=nv12,hwupload -c:v hevc_vaapi -qp 34
probe "h264_vaapi" -init_hw_device "vaapi=va:$DEV" -filter_hw_device va \
  -f lavfi -i "$SRC" -vf format=nv12,hwupload -c:v h264_vaapi -qp 34

hr "7. 怎么读这份报告"
cat <<'EOF'
· 第 2 步看不到 renderD128 → 容器没拿到 GPU。Unraid 模板里要加的是 Device（不是 Path），
  名称和值都填 /dev/dri，保存后重建容器（只点重启不生效）。
· 第 3 步报 permission denied → 加 --group-add video，或先临时用 --privileged 验证。
· 第 4 步 libmfxhw64 缺失 → 8~10 代核显的 QSV 一定失败，升级到 1.0.5 以上镜像。
· 第 6 步 qsv 失败但 vaapi 通过 → 正常，编码器选「自动」就会用 vaapi，同样是核显在编码，
  在 Unraid 面板看 Video 引擎占用率一样会上来。
· 第 6 步全部失败 → 把这份完整输出发给开发者。
EOF
