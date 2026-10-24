# ---------- Stage 1: 取 ffmpeg 静态构建 ----------
# BtbN 的 GPL 构建包含 libsvtav1 / libaom / libx265 / libx264 / libopus
# 以及 nvenc / qsv(libvpl) / vaapi 全套硬件编码器
FROM alpine:3.20 AS ffmpeg

ARG TARGETARCH=amd64
ARG FFMPEG_TAG=latest

RUN apk add --no-cache curl xz tar

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) FLAVOR="linux64" ;; \
      arm64) FLAVOR="linuxarm64" ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_TAG}/ffmpeg-master-${FFMPEG_TAG}-${FLAVOR}-gpl.tar.xz"; \
    echo "downloading $URL"; \
    curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 30 "$URL" -o /tmp/ffmpeg.tar.xz; \
    mkdir -p /opt/ffmpeg; \
    tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1; \
    rm -f /tmp/ffmpeg.tar.xz; \
    ls -la /opt/ffmpeg/bin; \
    /opt/ffmpeg/bin/ffmpeg -hide_banner -version | head -n 2


# ---------- Stage 2: 运行镜像 ----------
FROM python:3.12-slim-bookworm

ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/ffmpeg/bin:$PATH \
    FFMPEG_BIN=/opt/ffmpeg/bin/ffmpeg \
    FFPROBE_BIN=/opt/ffmpeg/bin/ffprobe

# 运行时依赖：
#  libva* / libvpl2     -> VAAPI、QSV 的运行时（dispatcher）
#  libmfx1              -> 旧版 Media SDK 运行时（libmfxhw64.so.1.35）：
#                          8~10 代核显（Skylake / Kaby Lake / Coffee Lake / Comet Lake）
#                          的 QSV 只能走它，缺了就会报 "Error initializing an internal MFX session"
#  libmfx-gen1.2        -> oneVPL GPU 运行时：11 代（Tiger Lake）及更新平台走它
#  两者由 libvpl dispatcher 按 GPU 代际自动选择，同时装上才能一个镜像通吃
#  intel-media-driver  -> Intel Gen8+ 的 VAAPI 驱动（含编码器）
#  mesa-va-drivers     -> AMD 的 VAAPI 驱动
#  vainfo              -> 诊断工具
#  注意：本工具只做软字幕轨（不烧录画面），所以不需要额外中文字体
RUN set -eux; \
    if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi; \
    sed -i -e "s/^Components: .*/Components: main contrib non-free non-free-firmware/" \
           /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    apt-get -o Acquire::Retries=5 update; \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        libva2 libva-drm2 libvpl2 vainfo \
        libmfx1 libmfx-gen1.2 \
        mesa-va-drivers \
        tzdata ca-certificates curl; \
    (apt-get -o Acquire::Retries=5 install -y --no-install-recommends intel-media-va-driver-non-free \
      || apt-get -o Acquire::Retries=5 install -y --no-install-recommends intel-media-va-driver); \
    rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg  /opt/ffmpeg/bin/ffmpeg
COPY --from=ffmpeg /opt/ffmpeg/bin/ffprobe /opt/ffmpeg/bin/ffprobe
RUN chmod +x /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe \
 && ln -sf /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg \
 && ln -sf /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe \
 && ffmpeg -hide_banner -version | head -n 1

WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /srv/requirements.txt

COPY app /srv/app
COPY tools/hw-doctor.sh /usr/local/bin/hw-doctor
RUN chmod +x /usr/local/bin/hw-doctor

RUN mkdir -p /media /output /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
