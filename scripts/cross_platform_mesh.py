#!/usr/bin/env python3
"""Cross-runner AgentWeb install and all-to-all routing smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERIFY = "agentwebadmin"
PLATFORMS = ("linux-x64", "linux-arm64", "macos-arm64", "windows-x64")


def request(url: str, payload: dict | None = None, headers: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def get_json(url: str, headers: dict | None = None) -> dict:
    status, _, body = request(url, headers=headers)
    if status != 200:
        raise RuntimeError(f"GET {url} returned HTTP {status}: {body[:300]!r}")
    return json.loads(body)


def download(url: str, path: pathlib.Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "awrelease-mesh/1"})
    with urllib.request.urlopen(req, timeout=120) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_json(url: str, process: subprocess.Popen, timeout: int = 60) -> dict:
    deadline = time.monotonic() + timeout
    last = "not started"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before readiness: rc={process.returncode}")
        try:
            return get_json(url)
        except Exception as error:
            last = str(error)
        time.sleep(1)
    raise RuntimeError(f"readiness timeout for {url}: {last}")


def artifact_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-x64-musl"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "linux-arm64-musl"
    if system == "Darwin" and machine in {"aarch64", "arm64"}:
        return "macos-arm64"
    if system == "Windows" and machine in {"x86_64", "amd64"}:
        return "windows-x64"
    raise RuntimeError(f"unsupported runner {system}/{machine}")


def download_agentgw(channel: str, directory: pathlib.Path) -> pathlib.Path:
    manifest = get_json(
        f"https://github.com/yxsicd/awrelease/releases/download/{channel}/agentgw-{channel}.json"
    )
    key = artifact_key()
    artifact = manifest["artifacts"][key]
    binary = directory / artifact["filename"]
    download(artifact.get("downloadUrl") or artifact["url"], binary)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != artifact["sha256"]:
        raise RuntimeError("AgentGW SHA-256 mismatch")
    if binary.stat().st_size != artifact["size"]:
        raise RuntimeError("AgentGW size mismatch")
    if platform.system() != "Windows":
        binary.chmod(0o755)
    return binary


def start_detached(argv: list[str], env: dict[str, str], log: pathlib.Path) -> subprocess.Popen:
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["RUNNER_TRACKING_ID"] = ""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
    return subprocess.Popen(
        argv,
        env=child_env,
        stdout=log.open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=platform.system() != "Windows",
        creationflags=flags,
    )


def coordinator_start(state_dir: pathlib.Path, channel: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    cloudflared = state_dir / "cloudflared"
    download(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        cloudflared,
    )
    cloudflared.chmod(0o755)
    tunnel_log = state_dir / "cloudflared.log"
    tunnel = start_detached(
        [str(cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        {},
        tunnel_log,
    )
    deadline = time.monotonic() + 90
    public_url = ""
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    while time.monotonic() < deadline and tunnel.poll() is None:
        match = pattern.search(tunnel_log.read_text(errors="ignore") if tunnel_log.exists() else "")
        if match:
            public_url = match.group(0)
            break
        time.sleep(1)
    if not public_url:
        raise RuntimeError(f"Cloudflare quick tunnel did not start: {tunnel_log.read_text(errors='ignore')[-1000:]}")

    topology = {
        "rgws": [{"id": "github-actions", "publicUrl": public_url}],
        "registrationPolicies": [{
            "id": "personal-default", "domainId": "ci",
            "gatewayClusterIds": ["11111111-1111-4111-8111-111111111111"],
            "publicGatewayIds": ["github-actions"],
        }],
        "enrollmentIssuers": [{
            "id": "ci", "gatewayInstanceIds": ["github-actions"],
            "allowedDomainIds": ["ci"],
            "allowedRegistrationPolicyIds": ["personal-default"],
        }],
    }
    topology_path = state_dir / "topology.json"
    topology_path.write_text(json.dumps(topology), encoding="utf-8")
    binary = download_agentgw(channel, state_dir)
    gateway = start_detached(
        [str(binary)],
        {
            "AGENTGW_MODE": "remote",
            "AGENTGW_BIND": f"127.0.0.1:{port}",
            "AGENTWEB_VERIFY": VERIFY,
            "AGENTGW_GATEWAY_ID": "11111111-1111-4111-8111-111111111111",
            "AGENTGW_GATEWAY_CLUSTER_ID": "11111111-1111-4111-8111-111111111111",
            "AGENTGW_LOGICAL_GATEWAY_ID": "github-actions",
            "AGENTGW_ENROLLMENT_ISSUER_ID": "ci",
            "AGENTGW_ENROLLMENT_SIGNING_KEY": "github-actions-ephemeral-signing-key-0001",
            "AGENTGW_REQUIRE_ENROLLMENT_TOKEN": "true",
            "AGENTGW_HTTP_UPSTREAM_ENABLED": "1",
            "AGENTGW_SETUP_SELF_SERVICE_POLICY_IDS": "personal-default",
            "AGENTGW_TOPOLOGY_FILE": str(topology_path),
            "AGENTWEB_RELEASE_CHANNEL": channel,
            "AGENTWEB_PUBLIC_URL": public_url,
            "AGENTGW_DIST_DIR": str(state_dir / "dist"),
        },
        state_dir / "agentgw.log",
    )
    wait_json(f"http://127.0.0.1:{port}/health", gateway)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if get_json(f"{public_url}/health").get("service") == "agentgw":
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("public quick tunnel did not reach AgentGW")
    (state_dir / "endpoint.json").write_text(
        json.dumps({"url": public_url, "channel": channel, "platforms": PLATFORMS}, indent=2) + "\n",
        encoding="utf-8",
    )
    (state_dir / "pids.json").write_text(json.dumps({"gateway": gateway.pid, "tunnel": tunnel.pid}))
    print(public_url)


def route(base: str, peer: str, name: str, payload: dict) -> dict:
    status, _, body = request(
        f"{base}/api/cmd",
        {"to": peer, "name": name, "payload": payload, "timeoutMs": 20000},
        {"Content-Type": "application/json", "x-agentweb-rgw-token": VERIFY},
    )
    value = json.loads(body)
    if status != 200 or value.get("ok") is not True:
        raise RuntimeError(f"{name} to {peer} failed: HTTP {status} {value}")
    if value.get("targetPeerId") != peer or value.get("routeDecision") != "peer_direct":
        raise RuntimeError(f"{name} to {peer} lacked exact route proof: {value}")
    return value.get("result", {})


def peer_platform(peer: dict) -> str | None:
    device = str((peer.get("metadata") or {}).get("deviceName") or "")
    for item in PLATFORMS:
        if device.startswith(f"gha-{item}-"):
            return item
    return None


def client_test(endpoint: pathlib.Path, source_platform: str, output: pathlib.Path) -> None:
    base = json.loads(endpoint.read_text())["url"].rstrip("/")
    headers = {"x-agentweb-rgw-token": VERIFY}
    deadline = time.monotonic() + 300
    selected: list[dict] = []
    while time.monotonic() < deadline:
        peers = get_json(f"{base}/api/peers", headers=headers).get("peers", [])
        selected = [p for p in peers if peer_platform(p)]
        by_platform = {item: [p for p in selected if peer_platform(p) == item] for item in PLATFORMS}
        if all(len(items) == 3 for items in by_platform.values()):
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"cross-platform peers incomplete: {[(p.get('id'), peer_platform(p)) for p in selected]}")

    checks = 0
    marker_base = f"AWMESH_{source_platform}_{os.environ.get('GITHUB_RUN_ID', 'local')}"
    for peer in selected:
        target_platform = peer_platform(peer)
        peer_id = peer["id"]
        status = route(base, peer_id, "admin.status", {})
        if status.get("ok") is not True:
            raise RuntimeError(f"admin.status failed for {peer_id}")
        checks += 1
        marker = f"{marker_base}_{peer_id[-6:]}"
        if target_platform == "windows-x64":
            command = "powershell.exe"
            args = ["-NoProfile", "-NonInteractive", "-Command", f"[Console]::Write('{marker}')"]
            file_path = f"C:\\Users\\runneradmin\\AppData\\Local\\Temp\\{marker}.txt"
        else:
            command = "/bin/sh"
            args = ["-c", f"printf %s {marker}"]
            file_path = f"/tmp/{marker}.txt"
        executed = route(base, peer_id, "admin.system.exec", {"command": command, "args": args, "sync": True, "timeout": 10})
        record = executed.get("record", {})
        if record.get("exitCode") != 0 or str(record.get("stdout", "")).strip() != marker:
            raise RuntimeError(f"exec mismatch for {peer_id}: {executed}")
        checks += 1
        route(base, peer_id, "admin.fs.write", {"path": file_path, "content": marker})
        checks += 1
        read_back = route(base, peer_id, "admin.fs.read", {"path": file_path})
        if read_back.get("content") != marker:
            raise RuntimeError(f"file mismatch for {peer_id}")
        checks += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "ok": True,
        "sourcePlatform": source_platform,
        "targetPlatforms": list(PLATFORMS),
        "targetPeerCount": len(selected),
        "routedCheckCount": checks,
        "routeDecision": "peer_direct",
    }, indent=2) + "\n")
    print(output.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("coordinator-start")
    start.add_argument("--state", type=pathlib.Path, required=True)
    start.add_argument("--channel", choices=("main", "prod"), default="prod")
    client = sub.add_parser("client-test")
    client.add_argument("--endpoint", type=pathlib.Path, required=True)
    client.add_argument("--platform", choices=PLATFORMS, required=True)
    client.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.command == "coordinator-start":
        coordinator_start(args.state, args.channel)
    else:
        client_test(args.endpoint, args.platform, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
