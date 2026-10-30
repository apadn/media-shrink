#!/usr/bin/env bash
# 构建 MediaShrink 镜像。
#
# 铁律：每次构建都必须同时打「版本号」和 latest 两个标签，且两者指向同一镜像 ID。
# 本脚本从 app/config.py 读版本号，构建后自动校验两个标签一致，不一致直接失败退出。
#
# 用法：
#   ./scripts/build.sh                 构建镜像（版本号 + latest）
#   ./scripts/build.sh --tar           构建并导出 dist/media-shrink-<版本>-amd64.tar（含双标签）+ sha256
#   ./scripts/build.sh --push          构建并推送两个标签（需要先 docker login）
#   ./scripts/build.sh --tar --push    构建、导出、再推送
#
# 环境变量：
#   NS          镜像命名空间，默认 xdxy
#   APT_MIRROR  apt 源，默认 mirrors.ustc.edu.cn（海外可设 deb.debian.org）
#   PIP_INDEX_URL  pip 源，默认 https://pypi.org/simple
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
用法：
  ./scripts/build.sh                 构建镜像（版本号 + latest）
  ./scripts/build.sh --tar           构建并导出 dist/media-shrink-<版本>-amd64.tar（含双标签）+ sha256
  ./scripts/build.sh --push          构建并推送两个标签（需要先 docker login）
  ./scripts/build.sh --tar --push    构建、导出、再推送

版本号取自 app/config.py 的 APP_VERSION，构建后自动校验两个标签指向同一镜像。

环境变量：
  NS             镜像命名空间，默认 xdxy
  APT_MIRROR     apt 源，默认 mirrors.ustc.edu.cn（海外可设 deb.debian.org）
  PIP_INDEX_URL  pip 源，默认 https://pypi.org/simple
EOF
}

NS="${NS:-xdxy}"
APT_MIRROR="${APT_MIRROR:-mirrors.ustc.edu.cn}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"

DO_TAR=0
DO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --tar)  DO_TAR=1 ;;
    --push) DO_PUSH=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：${arg}（可用 --tar / --push / --help）" >&2; exit 2 ;;
  esac
done

VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' app/config.py)"
if [ -z "$VERSION" ]; then
  echo "读不到 app/config.py 里的 APP_VERSION" >&2
  exit 1
fi

VER_TAG="$NS/media-shrink:$VERSION"
LATEST_TAG="$NS/media-shrink:latest"

echo "==> 版本：$VERSION"
echo "==> 标签：$VER_TAG  +  $LATEST_TAG"

# --provenance=false --sbom=false：不要 buildx 的 attestation 附加清单。
# 默认会额外塞一个「证明」清单进 tar，老版本 docker load 会把它变成一个没有标签的孤立镜像，
# 还容易让人以为 latest 丢了。关掉后包内就只有一个干净的镜像清单。
docker build \
  --provenance=false \
  --sbom=false \
  --build-arg "APT_MIRROR=$APT_MIRROR" \
  --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" \
  -t "$VER_TAG" \
  -t "$LATEST_TAG" \
  .

# 校验：两个标签必须指向同一个镜像 ID，否则说明哪个环节漏了 latest
ID_VER="$(docker image inspect "$VER_TAG" --format '{{.Id}}')"
ID_LATEST="$(docker image inspect "$LATEST_TAG" --format '{{.Id}}')"
if [ "$ID_VER" != "$ID_LATEST" ]; then
  echo "构建失败：$VER_TAG 与 $LATEST_TAG 不是同一个镜像" >&2
  echo "  $VER_TAG  -> $ID_VER" >&2
  echo "  $LATEST_TAG -> $ID_LATEST" >&2
  exit 1
fi
echo "==> 校验通过，两个标签同为 ${ID_VER#sha256:}（前 12 位 ${ID_VER:7:12}）"

if [ "$DO_TAR" = 1 ]; then
  OUT="dist/media-shrink-${VERSION}-amd64.tar"
  mkdir -p dist
  echo "==> 导出 ${OUT}（显式带上两个标签，docker load 后两者都在）"
  docker save "$VER_TAG" "$LATEST_TAG" -o "$OUT"
  shasum -a 256 "$OUT" | tee "$OUT.sha256"
  ls -lh "$OUT"
fi

if [ "$DO_PUSH" = 1 ]; then
  echo "==> 推送 $VER_TAG"
  docker push "$VER_TAG"
  echo "==> 推送 $LATEST_TAG"
  docker push "$LATEST_TAG"
  echo "==> 推送完成。到仓库里确认 latest 的 digest 与 $VERSION 一致，否则 latest 没生效。"
fi

echo "==> 完成：$VERSION"
