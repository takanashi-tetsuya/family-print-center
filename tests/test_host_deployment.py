"""Exercise host deployment ordering without Docker, mounts or host mutations."""
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
HOST = PROJECT / "deploy" / "host"
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.fixture
def host_tools(tmp_path):
    mount = tmp_path / "docker"
    data = mount / "data"
    data.mkdir(parents=True)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/mock {mount} ext4 rw,noatime 0 0\n")
    config = tmp_path / "tools.json"
    calls = tmp_path / "docker-calls.jsonl"
    values = {
        "uuid": UUID, "configured_root": str(data), "running_root": str(data),
        "available": 29000000, "buildx": True,
        "endpoint": "unix:///var/run/docker.sock",
    }
    config.write_text(json.dumps(values))
    guard = tmp_path / "installed-storage-ready"
    guard.write_text("#!/bin/sh\nexit 0\n")
    guard.chmod(0o755)
    init = tmp_path / "dockerd.init"
    init.write_text(f'start_service() {{\n    {guard} || {{ logger -t dockerd "Docker storage unavailable; refusing start"; return 1; }}\n    daemon_start\n}}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # All real dependencies remain read-only; only the system-facing tools are mocked.
    mock = f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
from pathlib import Path
values = json.loads(Path(os.environ["FPC_TEST_CONFIG"]).read_text())
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
if tool == "awk":
    if args[-1:] == ["/proc/mounts"]:
        args[-1] = os.environ["FPC_TEST_MOUNTS"]
    raise SystemExit(subprocess.call([os.environ["FPC_TEST_REAL_AWK"], *args]))
if tool == "uci":
    key = args[-1]
    result = {"fstab.docker.uuid": values.get("configured_uuid", values["uuid"]),
              "dockerd.globals.data_root": values["configured_root"],
              "dockerd.globals.alt_config_file": values.get("alt_config", "")}.get(key)
    if result is None or result == "":
        raise SystemExit(1)
    print(result)
elif tool == "block":
    print('/dev/mock: UUID="' + values["uuid"] + '" TYPE="ext4"')
elif tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted on")
    print("/dev/mock 30000000 1000000", values["available"], "4%", args[-1])
elif tool == "docker":
    with open(os.environ["FPC_TEST_CALLS"], "a") as output:
        output.write(json.dumps(args) + "\\n")
    if args[:1] == ["info"]:
        if values.get("daemon_down"):
            raise SystemExit(1)
        print(values["running_root"])
    elif args[:2] == ["context", "inspect"]:
        print(values["endpoint"])
    elif args[:2] == ["buildx", "version"]:
        raise SystemExit(0 if values["buildx"] else 1)
    elif args[:2] == ["compose", "version"]:
        raise SystemExit(1 if values.get("compose_missing") else 0)
    elif args[:2] == ["compose", "config"]:
        raise SystemExit(1 if values.get("compose_invalid") else 0)
'''
    for name in ("awk", "uci", "block", "df", "docker"):
        script = bin_dir / name
        script.write_text(mock)
        script.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "FPC_DOCKER_MOUNT": str(mount), "FPC_DOCKER_DATA_ROOT": str(data),
           "FPC_DOCKER_GUARD": str(guard), "FPC_DOCKER_INIT": str(init),
           "FPC_TEST_CONFIG": str(config), "FPC_TEST_CALLS": str(calls),
           "FPC_TEST_MOUNTS": str(mounts), "FPC_TEST_REAL_AWK": shutil.which("awk")}
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "FPC_DOCKER_UUID", "FPC_MIN_FREE_KB",
                "DEBIAN_IMAGE", "DOCKER_CLI_IMAGE", "EXTRA_DRIVER_PACKAGES"):
        env.pop(key, None)

    class Tools:
        def change(self, **updates):
            values.update(updates)
            config.write_text(json.dumps(values))

        def run(self, script, **updates):
            return subprocess.run(["sh", str(script)], env={**env, **updates},
                                  text=True, capture_output=True, timeout=10)

        def commands(self):
            return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    tools = Tools()
    tools.mount, tools.data, tools.mounts, tools.env = mount, data, mounts, env
    tools.guard, tools.init = guard, init
    return tools


def test_guard_and_preflight_are_read_only(host_tools):
    assert host_tools.run(HOST / "storage-ready.sh").returncode == 0
    result = host_tools.run(HOST / "preflight.sh")
    assert result.returncode == 0, result.stderr
    assert "部署前檢查通過" in result.stdout
    assert all(command[:1] in (["context"], ["info"], ["compose"]) for command in host_tools.commands())
    assert all(command[:2] != ["compose", "up"] for command in host_tools.commands())


@pytest.mark.parametrize("mount_options", [None, "ext4 ro,noatime", "tmpfs rw"])
def test_missing_or_wrong_mount_stops_update_before_docker(host_tools, mount_options):
    host_tools.mounts.write_text(
        "" if mount_options is None else f"/dev/mock {host_tools.mount} {mount_options} 0 0\n")
    result = host_tools.run(HOST / "update.sh")
    assert result.returncode == 1 and "可寫 ext4" in result.stderr
    assert host_tools.commands() == []
    assert list(host_tools.mount.iterdir()) == [host_tools.data]


@pytest.mark.parametrize("change,message", [
    ({"configured_uuid": "not-a-uuid"}, "預期分區 UUID"),
    ({"uuid": "11111111-2222-3333-4444-555555555555", "configured_uuid": UUID}, "UUID 不符"),
    ({"configured_root": "/overlay/docker"}, "data_root 必須"),
    ({"alt_config": "/etc/other-daemon.json"}, "alt_config_file"),
    ({"available": 1024}, "可用"),
    ({"running_root": "/overlay/docker"}, "執行中的 Docker"),
    ({"daemon_down": True}, "Docker 尚未正常啟動"),
    ({"compose_missing": True}, "Docker Compose"),
    ({"endpoint": "tcp://remote.example:2375"}, "本機 Docker socket"),
])
def test_preflight_failures_never_build_or_update(host_tools, change, message):
    host_tools.change(**change)
    result = host_tools.run(HOST / "update.sh")
    assert result.returncode == 1
    # The space error states the remaining amount, not a generic "可用" label.
    assert ("僅剩" if message == "可用" else message) in result.stderr
    assert not any(command[:1] in (["build"], ["run"]) or command[:2] == ["compose", "up"]
                   for command in host_tools.commands())


def test_guard_never_creates_missing_data_directory(host_tools):
    host_tools.data.rmdir()
    result = host_tools.run(HOST / "storage-ready.sh")
    assert result.returncode == 1 and "不建立目錄" in result.stderr
    assert not host_tools.data.exists()


def test_guard_rejects_data_symlink_to_system_partition(host_tools, tmp_path):
    host_tools.data.rmdir()
    outside = tmp_path / "system-data"
    outside.mkdir()
    host_tools.data.symlink_to(outside, target_is_directory=True)
    result = host_tools.run(HOST / "storage-ready.sh")
    assert result.returncode == 1 and "離開了指定分區" in result.stderr


def test_explicit_uuid_and_space_threshold_are_supported(host_tools):
    host_tools.change(configured_uuid="invalid", available=1024)
    result = host_tools.run(HOST / "preflight.sh", FPC_DOCKER_UUID=UUID.upper(), FPC_MIN_FREE_KB="512")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("init_text", [
    "start_service() {\n daemon_start\n}\n",
    "start_service() {\n # {guard} || {{ return 1; }}\n daemon_start\n}\n",
    "start_service() {\n daemon_start\n {guard} || {{ return 1; }}\n}\n",
    "start_service() {\n {guard}\n daemon_start\n}\n",
    'start_service() {\n {guard} || {{ logger "return 1"; }}\n daemon_start\n}\n',
    "start_service() {\n {guard} || {{ return 1; }} || true\n daemon_start\n}\n",
])
def test_missing_late_commented_or_fail_open_hook_stops_update(host_tools, init_text):
    host_tools.init.write_text(init_text.replace("{guard}", str(host_tools.guard)))
    result = host_tools.run(HOST / "update.sh")
    assert result.returncode == 1 and "第一個有效行" in result.stderr
    assert host_tools.commands() == []


@pytest.mark.parametrize("form", [
    'start_service() {{\n # comment\n "{guard}" || {{ return 1; }}\n daemon_start\n}}\n',
    'start_service()\n{{\n {guard} || {{ return 1; }}\n daemon_start\n}}\n',
])
def test_installed_hook_accepts_comments_and_standard_brace_layouts(host_tools, form):
    host_tools.init.write_text(form.format(guard=host_tools.guard))
    result = host_tools.run(HOST / "preflight.sh")
    assert result.returncode == 0, result.stderr


def test_missing_or_nonexecutable_installed_guard_stops_update(host_tools):
    host_tools.guard.chmod(0o644)
    result = host_tools.run(HOST / "update.sh")
    assert result.returncode == 1 and "人工安裝" in result.stderr
    assert host_tools.commands() == []


def test_update_checks_compose_then_builds_with_official_mirror_before_up(host_tools):
    result = host_tools.run(HOST / "update.sh")
    assert result.returncode == 0, result.stderr
    commands = host_tools.commands()
    build_index = next(index for index, command in enumerate(commands) if command[:1] == ["build"])
    config_index = commands.index(["compose", "config", "--quiet"])
    up_index = commands.index(["compose", "up", "-d", "--no-build"])
    assert config_index < build_index < up_index
    assert "DEBIAN_IMAGE=public.ecr.aws/docker/library/debian:trixie-slim" in commands[build_index]
    assert commands[0][:2] == ["context", "inspect"]


def test_invalid_compose_stops_before_build(host_tools):
    host_tools.change(compose_invalid=True)
    result = host_tools.run(HOST / "update.sh")
    assert result.returncode == 1 and "Compose 設定不正確" in result.stderr
    assert not any(command[:1] == ["build"] or command[:2] == ["compose", "up"] for command in host_tools.commands())


def test_generic_build_keeps_native_buildx_and_image_override(host_tools):
    result = host_tools.run(PROJECT / "deploy" / "build-image.sh", DEBIAN_IMAGE="registry.example/debian:custom")
    assert result.returncode == 0, result.stderr
    commands = host_tools.commands()
    assert commands[-1][0] == "build"
    assert "DEBIAN_IMAGE=registry.example/debian:custom" in commands[-1]
    assert not any(command[:1] == ["run"] for command in commands)


def test_missing_buildx_uses_the_configured_local_socket_and_cli_image(host_tools, tmp_path):
    endpoint = tmp_path / "docker.sock"
    with socket.socket(socket.AF_UNIX) as local_socket:
        local_socket.bind(str(endpoint))
        host_tools.change(buildx=False, endpoint="unix://" + str(endpoint))
        result = host_tools.run(PROJECT / "deploy" / "build-image.sh", DOCKER_CLI_IMAGE="official.example/docker:cli")
    assert result.returncode == 0, result.stderr
    call = host_tools.commands()[-1]
    assert call[:2] == ["run", "--rm"]
    assert f"{endpoint}:/var/run/docker.sock" in call
    assert "official.example/docker:cli" in call
    assert "DOCKER_HOST=unix:///var/run/docker.sock" in call


@pytest.mark.parametrize("endpoint,message", [
    ("tcp://remote.example:2375", "遠端 Docker"),
    ("unix:///nonexistent/fpc-docker.sock", "socket 不存在"),
])
def test_buildx_fallback_fails_clearly_for_unusable_endpoints(host_tools, endpoint, message):
    host_tools.change(buildx=False, endpoint=endpoint)
    result = host_tools.run(PROJECT / "deploy" / "build-image.sh")
    assert result.returncode == 1 and message in result.stderr
    assert not any(command[:1] == ["run"] for command in host_tools.commands())
