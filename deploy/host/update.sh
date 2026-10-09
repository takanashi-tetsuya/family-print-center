#!/bin/sh
# Run on OpenWrt after copying the project onto its prepared Docker partition.
# No partitioning, formatting, directory creation or host-network changes.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)
sh "$script_dir/preflight.sh"
cd "$project_dir"
if ! docker compose config --quiet; then
    printf '更新已停止：Compose 設定不正確，請先修正專案部署設定。\n' >&2
    exit 1
fi
DEBIAN_IMAGE="${DEBIAN_IMAGE:-public.ecr.aws/docker/library/debian:trixie-slim}" \
DOCKER_CLI_IMAGE="${DOCKER_CLI_IMAGE:-public.ecr.aws/docker/library/docker:29-cli}" \
sh deploy/build-image.sh
docker compose up -d --no-build
