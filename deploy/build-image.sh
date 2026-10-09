#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

# Optional Debian driver packages, e.g. EXTRA_DRIVER_PACKAGES="printer-driver-cups-pdf".
extra_drivers="EXTRA_DRIVER_PACKAGES=${EXTRA_DRIVER_PACKAGES:-}"
debian_image="DEBIAN_IMAGE=${DEBIAN_IMAGE:-debian:trixie-slim}"
cli_image="${DOCKER_CLI_IMAGE:-docker:29-cli}"

if ! command -v docker >/dev/null 2>&1; then
    printf '無法建置：找不到 Docker；請先安裝並啟動 Docker 引擎。\n' >&2
    exit 1
fi
if ! docker info --format '{{.DockerRootDir}}' >/dev/null 2>&1; then
    printf '無法建置：Docker 引擎沒有回應；OpenWrt 請先執行 deploy/host/preflight.sh 檢查永久儲存。\n' >&2
    exit 1
fi

# Downloaded fonts stay in deploy/fonts/cache; only missing ones are fetched.
sh deploy/fonts/fetch.sh || printf '部分字體沒有下載成功，映像照樣建置，只是少了這些字體。\n' >&2

if docker buildx version >/dev/null 2>&1; then
    docker build --build-arg "$extra_drivers" --build-arg "$debian_image" -t family-print-center:local .
else
    # OpenWrt packages the engine and Compose separately from Buildx. The
    # official CLI image includes Buildx and uses the already-running engine.
    # Only this temporary build helper receives the Docker socket.
    if [ -n "${DOCKER_CONTEXT:-}" ]; then
        endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || {
            printf '無法建置：無法確認 Docker context。\n' >&2; exit 1;
        }
    elif [ -n "${DOCKER_HOST:-}" ]; then
        endpoint="$DOCKER_HOST"
    else
        endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || {
            printf '無法建置：無法確認 Docker context。\n' >&2; exit 1;
        }
    fi
    case "$endpoint" in unix:///*) docker_socket="${endpoint#unix://}" ;; *)
        printf '無法使用臨時 Buildx：備援只支援本機 Unix socket；遠端 Docker 請先安裝本機 Buildx。\n' >&2
        exit 1;;
    esac
    if [ ! -S "$docker_socket" ]; then
        printf '無法使用臨時 Buildx：Docker socket 不存在；請檢查本機 Docker context。\n' >&2
        exit 1
    fi
    docker run --rm --network host \
        --env DOCKER_HOST=unix:///var/run/docker.sock \
        --volume "$docker_socket:/var/run/docker.sock" \
        --volume "$project_dir:/workspace:ro" \
        --workdir /workspace \
        "$cli_image" docker build --build-arg "$extra_drivers" --build-arg "$debian_image" -t family-print-center:local .
fi
