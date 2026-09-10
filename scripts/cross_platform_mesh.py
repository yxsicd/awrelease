#!/usr/bin/env python3
"""Cross-runner AgentWeb install, redundant-GW, and routing smoke."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

VERIFY = "agentwebadmin"
PLATFORMS = ("linux-x64", "linux-arm64", "macos-arm64", "windows-x64")
CLUSTER_ID = "11111111-1111-4111-8111-111111111111"
SIGNING_KEY = "github-actions-ephemeral-signing-key-0001"


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
    req = urllib.request.Request(url, headers={"User-Agent": "awrelease-mesh/2"})
    with urllib.request.urlopen(req, timeout=120) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_json(url: str, process: subprocess.Popen | None = None, timeout: int = 60) -> dict:
    deadline = time.monotonic() + timeout
    last = "not started"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
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
    if platform.system() == "Darwin":
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(binary)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    return binary


def download_cloudflared(directory: pathlib.Path) -> pathlib.Path:
    key = artifact_key()
    assets = {
        "linux-x64-musl": "cloudflared-linux-amd64",
        "linux-arm64-musl": "cloudflared-linux-arm64",
        "macos-arm64": "cloudflared-darwin-arm64.tgz",
        "windows-x64": "cloudflared-windows-amd64.exe",
    }
    asset = assets[key]
    downloaded = directory / asset
    download(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}", downloaded)
    if asset.endswith(".tgz"):
        with tarfile.open(downloaded, "r:gz") as archive:
            member = next(item for item in archive.getmembers() if pathlib.PurePosixPath(item.name).name == "cloudflared")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("cloudflared archive has no binary")
            binary = directory / "cloudflared"
            with source, binary.open("wb") as output:
                shutil.copyfileobj(source, output)
    else:
        binary = directory / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
        if downloaded != binary:
            downloaded.replace(binary)
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


def gateway_id(platform_name: str) -> str:
    index = PLATFORMS.index(platform_name) + 1
    return f"11111111-1111-4111-8111-{index:012d}"


def logical_gateway_id(platform_name: str) -> str:
    return f"gha-{platform_name}"


def write_topology(path: pathlib.Path, endpoints: list[dict]) -> None:
    gateway_ids = [logical_gateway_id(item["platform"]) for item in endpoints]
    topology = {
        "schemaVersion": 2,
        "kind": "agentweb.current-topology",
        "gatewayClusters": [{"id": CLUSTER_ID, "name": "GitHub Actions ephemeral mesh"}],
        "rgws": [
            {
                "id": logical_gateway_id(item["platform"]),
                "publicUrl": item["url"],
                "wsUrl": f"{item['url'].replace('https://', 'wss://', 1)}/ws?role=upstream",
            }
            for item in endpoints
        ],
        "registrationPolicies": [{
            "id": "personal-default",
            "domainId": "ci",
            "gatewayClusterIds": [CLUSTER_ID],
            "publicGatewayIds": gateway_ids,
        }],
        "enrollmentIssuers": [{
            "id": "ci",
            "gatewayInstanceIds": gateway_ids,
            "allowedDomainIds": ["ci"],
            "allowedRegistrationPolicyIds": ["personal-default"],
        }],
    }
    path.write_text(json.dumps(topology, indent=2) + "\n", encoding="utf-8")


def gateway_environment(
    state_dir: pathlib.Path,
    channel: str,
    platform_name: str,
    public_url: str,
    port: int,
    mode: str,
    remote_gws: list[str],
    enrollment_token: str = "",
) -> dict[str, str]:
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    return {
        "AGENTGW_MODE": mode,
        "AGENTGW_BIND": f"127.0.0.1:{port}",
        "AGENTWEB_VERIFY": VERIFY,
        "AGENTGW_GATEWAY_ID": gateway_id(platform_name),
        "AGENTGW_GATEWAY_CLUSTER_ID": CLUSTER_ID,
        "AGENTGW_LOGICAL_GATEWAY_ID": logical_gateway_id(platform_name),
        "AGENTGW_ENROLLMENT_ISSUER_ID": "ci",
        "AGENTGW_ENROLLMENT_SIGNING_KEY": SIGNING_KEY,
        "AGENTGW_REQUIRE_ENROLLMENT_TOKEN": "true",
        "AGENTGW_HTTP_UPSTREAM_ENABLED": "1",
        "AGENTGW_SETUP_SELF_SERVICE_POLICY_IDS": "personal-default",
        "AGENTGW_TOPOLOGY_FILE": str(state_dir / "topology.json"),
        "AGENTWEB_RELEASE_CHANNEL": channel,
        "AGENTWEB_PUBLIC_URL": public_url,
        "AGENTGW_DIST_DIR": str(state_dir / "dist"),
        "AGENTGW_NODE_ID_FILE": str(state_dir / "gateway-node-id"),
        "AGENTGW_DEVICE_ID_FILE": str(state_dir / "gateway-device-id"),
        "AGENTGW_DEVICE_NAME": f"gha-gateway-{platform_name}-{run_id}",
        "AGENTGW_NODE_NAME": f"gha-gateway-{platform_name}-{run_id}",
        "AGENTGW_REMOTE_GWS": ",".join(remote_gws),
        "AGENTGW_UPSTREAM_MODE": "all",
        "AGENTGW_UPSTREAM_TRANSPORT": "auto",
        "AGENTGW_ENROLLMENT_TOKEN": enrollment_token,
        "AGENTGW_MANAGEMENT_DOMAIN_ID": "ci" if enrollment_token else "",
        "AGENTGW_REGISTRATION_POLICY_ID": "personal-default" if enrollment_token else "",
        "AGENTGW_ENROLLMENT_PROFILE": "production" if enrollment_token else "",
        "AGENTGW_NO_SETUP_BROWSER": "true",
    }


def write_haproxy_config(path: pathlib.Path, bind_port: int, backend_ports: dict[str, int]) -> None:
    lines = [
        "global", "  maxconn 2048", "defaults", "  mode http", "  timeout connect 10s",
        "  timeout client 5m", "  timeout server 5m", "frontend mesh", f"  bind 127.0.0.1:{bind_port}",
    ]
    for item in PLATFORMS:
        safe = item.replace("-", "_")
        lines.extend([f"  acl path_{safe} path_beg /{item}", f"  use_backend gw_{safe} if path_{safe}"])
    for item in PLATFORMS:
        safe = item.replace("-", "_")
        lines.extend([
            f"backend gw_{safe}",
            f"  http-request set-path %[path,regsub(^/{item},)]",
            f"  server gw_{safe} 127.0.0.1:{backend_ports[item]}",
        ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def gateway_cluster_start(state_dir: pathlib.Path, channel: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    binary = download_agentgw(channel, state_dir)
    cloudflared = download_cloudflared(state_dir)
    backend_ports = {item: free_port() for item in PLATFORMS}
    proxy_port = free_port()
    haproxy_config = state_dir / "haproxy.cfg"
    write_haproxy_config(haproxy_config, proxy_port, backend_ports)
    proxy = start_detached(["haproxy", "-f", str(haproxy_config), "-db"], {}, state_dir / "haproxy.log")
    tunnel_log = state_dir / "cloudflared.log"
    tunnel = start_detached(
        [str(cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{proxy_port}"], {}, tunnel_log,
    )
    gateways: dict[str, subprocess.Popen] = {}
    try:
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
        endpoints = [{"url": f"{public_url}/{item}", "channel": channel, "platform": item} for item in PLATFORMS]
        for endpoint in endpoints:
            item = endpoint["platform"]
            gateway_dir = state_dir / item
            gateway_dir.mkdir()
            write_topology(gateway_dir / "topology.json", endpoints)
            env = gateway_environment(gateway_dir, channel, item, endpoint["url"], backend_ports[item], "all-in-one", [])
            gateway = start_detached([str(binary)], env, gateway_dir / "agentgw.log")
            gateways[item] = gateway
            wait_json(f"http://127.0.0.1:{backend_ports[item]}/health", gateway)
        for endpoint in endpoints:
            wait_json(f"{endpoint['url']}/health", timeout=90)

        for endpoint in endpoints:
            item = endpoint["platform"]
            gateway_dir = state_dir / item
            device_name = f"gha-gateway-{item}-{os.environ.get('GITHUB_RUN_ID', 'local')}"
            token = issue_device_token(endpoint["url"], device_name)
            stop_pid(gateways[item].pid)
            remote_gws = [
                f"{candidate['url'].replace('https://', 'wss://', 1)}/ws?role=upstream"
                for candidate in endpoints if candidate != endpoint
            ]
            env = gateway_environment(
                gateway_dir, channel, item, endpoint["url"], backend_ports[item], "all-in-one", remote_gws, token,
            )
            (gateway_dir / "gateway-env.json").write_text(json.dumps(env), encoding="utf-8")
            gateway = start_detached([str(binary)], env, gateway_dir / "agentgw.log")
            gateways[item] = gateway
            wait_json(f"http://127.0.0.1:{backend_ports[item]}/health", gateway)
            wait_json(f"{endpoint['url']}/health", timeout=90)

        bundle = {"channel": channel, "gateways": endpoints, "platforms": list(PLATFORMS)}
        (state_dir / "endpoint.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        (state_dir / "runtime.json").write_text(json.dumps({
            "tunnelPid": tunnel.pid, "proxyPid": proxy.pid, "binary": str(binary), "channel": channel,
            "publicUrl": public_url,
            "gateways": [{"platform": item, "gatewayPid": gateways[item].pid, "port": backend_ports[item]} for item in PLATFORMS],
        }, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(bundle))
    except Exception:
        for gateway in gateways.values():
            if gateway.poll() is None:
                stop_pid(gateway.pid)
        if tunnel.poll() is None:
            stop_pid(tunnel.pid)
        if proxy.poll() is None:
            stop_pid(proxy.pid)
        raise


def read_endpoints(path: pathlib.Path) -> list[dict]:
    if path.is_dir():
        path = path / "endpoint.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    endpoints = bundle.get("gateways", [])
    if [item.get("platform") for item in endpoints] != list(PLATFORMS):
        raise RuntimeError(f"endpoint bundle does not contain the four platforms: {bundle}")
    if not all(str(item.get("url", "")).startswith("https://") for item in endpoints):
        raise RuntimeError(f"endpoint bundle contains a non-HTTPS gateway: {bundle}")
    return endpoints


def stop_pid(pid: int) -> None:
    if platform.system() == "Windows":
        subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    if platform.system() != "Windows":
        os.kill(pid, signal.SIGKILL)


def issue_device_token(public_url: str, device_name: str) -> str:
    basic = base64.b64encode(f"agentweb:{VERIFY}".encode()).decode()
    status, _, body = request(
        f"{public_url}/setup/api/enrollment/claims",
        {"deviceName": device_name, "registrationPolicyId": "personal-default", "profile": "production"},
        {"Content-Type": "application/json", "Authorization": f"Basic {basic}"},
    )
    response = json.loads(body)
    if status != 201 or response.get("ok") is not True:
        raise RuntimeError(f"gateway claim failed: HTTP {status} {response}")
    manifest = get_json(response["claimUrl"])
    token = str(manifest.get("enrollmentToken") or "")
    if not token:
        raise RuntimeError("gateway enrollment manifest omitted enrollmentToken")
    return token


def route(base: str, peer: str, name: str, payload: dict) -> dict:
    status, _, body = request(
        f"{base}/api/cmd", {"to": peer, "name": name, "payload": payload, "timeoutMs": 20000},
        {"Content-Type": "application/json", "x-agentweb-rgw-token": VERIFY},
    )
    value = json.loads(body)
    if status != 200 or value.get("ok") is not True:
        raise RuntimeError(f"{name} to {peer} failed through {base}: HTTP {status} {value}")
    if value.get("targetPeerId") != peer or value.get("routeDecision") != "peer_direct":
        raise RuntimeError(f"{name} to {peer} lacked exact route proof through {base}: {value}")
    return value.get("result", {})


def peer_platform(peer: dict) -> str | None:
    device = str((peer.get("metadata") or {}).get("deviceName") or "")
    for item in PLATFORMS:
        if device.startswith(f"gha-{item}-"):
            return item
    return None


def gateway_peer_platform(peer: dict) -> str | None:
    device = str((peer.get("metadata") or {}).get("deviceName") or "")
    for item in PLATFORMS:
        if device.startswith(f"gha-gateway-{item}-"):
            return item
    return None


def wait_for_gateway_peers(base: str, own_platform: str, timeout: int = 180) -> list[dict]:
    headers = {"x-agentweb-rgw-token": VERIFY}
    expected = set(PLATFORMS) - {own_platform}
    deadline = time.monotonic() + timeout
    selected: list[dict] = []
    while time.monotonic() < deadline:
        peers = get_json(f"{base}/api/peers", headers=headers).get("peers", [])
        selected = [peer for peer in peers if gateway_peer_platform(peer)]
        if {gateway_peer_platform(peer) for peer in selected} == expected and len(selected) == 3:
            return selected
        time.sleep(2)
    raise RuntimeError(f"all-in-one gateway peers incomplete at {base}: {[(p.get('id'), gateway_peer_platform(p)) for p in selected]}")


def wait_for_mabc(base: str, timeout: int = 300) -> list[dict]:
    headers = {"x-agentweb-rgw-token": VERIFY}
    deadline = time.monotonic() + timeout
    selected: list[dict] = []
    while time.monotonic() < deadline:
        peers = get_json(f"{base}/api/peers", headers=headers).get("peers", [])
        selected = [peer for peer in peers if peer_platform(peer)]
        by_platform = {item: [peer for peer in selected if peer_platform(peer) == item] for item in PLATFORMS}
        if all(len(items) == 3 for items in by_platform.values()):
            return selected
        time.sleep(2)
    raise RuntimeError(f"cross-platform peers incomplete at {base}: {[(p.get('id'), peer_platform(p)) for p in selected]}")


def command_payload(marker: str, target_platform: str) -> tuple[str, list[str], str]:
    if target_platform == "windows-x64":
        return "powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", f"[Console]::Write('{marker}')"], f"C:\\Users\\runneradmin\\AppData\\Local\\Temp\\{marker}.txt"
    return "/bin/sh", ["-c", f"printf %s {marker}"], f"/tmp/{marker}.txt"


def verify_exec_and_file(base: str, peer: dict, marker: str) -> int:
    peer_id = peer["id"]
    target_platform = peer_platform(peer)
    if target_platform is None:
        raise RuntimeError(f"unknown target platform for {peer_id}")
    command, args, file_path = command_payload(marker, target_platform)
    executed = route(base, peer_id, "admin.system.exec", {"command": command, "args": args, "sync": True, "timeout": 10})
    record = executed.get("record", {})
    if record.get("exitCode") != 0 or str(record.get("stdout", "")).strip() != marker:
        raise RuntimeError(f"exec mismatch for {peer_id}: {executed}")
    route(base, peer_id, "admin.fs.write", {"path": file_path, "content": marker})
    read_back = route(base, peer_id, "admin.fs.read", {"path": file_path})
    if read_back.get("content") != marker:
        raise RuntimeError(f"file mismatch for {peer_id}")
    return 3


def client_test(endpoints_root: pathlib.Path, source_platform: str, output: pathlib.Path) -> None:
    endpoints = read_endpoints(endpoints_root)
    checks = 0
    peer_sets: dict[str, list[dict]] = {}
    gateway_peer_sets: dict[str, list[dict]] = {}
    marker_base = f"AWMESH_{source_platform}_{os.environ.get('GITHUB_RUN_ID', 'local')}"
    for endpoint in endpoints:
        base = endpoint["url"].rstrip("/")
        selected = wait_for_mabc(base)
        peer_sets[endpoint["platform"]] = selected
        gateway_peers = wait_for_gateway_peers(base, endpoint["platform"])
        gateway_peer_sets[endpoint["platform"]] = gateway_peers
        for peer in gateway_peers:
            status = route(base, peer["id"], "admin.status", {})
            if status.get("ok") is not True:
                raise RuntimeError(f"all-in-one peer status failed for {peer['id']} through {base}")
            checks += 1
        for peer in selected:
            status = route(base, peer["id"], "admin.status", {})
            if status.get("ok") is not True:
                raise RuntimeError(f"admin.status failed for {peer['id']} through {base}")
            checks += 1
        for target_platform in PLATFORMS:
            peer = next(item for item in selected if peer_platform(item) == target_platform)
            marker = f"{marker_base}_{endpoint['platform']}_{target_platform}".replace("-", "_")
            checks += verify_exec_and_file(base, peer, marker)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "ok": True, "sourcePlatform": source_platform, "gatewayPlatforms": list(peer_sets),
        "gatewayCount": len(peer_sets), "targetPlatforms": list(PLATFORMS),
        "gatewayPeerCountPerGateway": {key: len(value) for key, value in gateway_peer_sets.items()},
        "targetPeerCountPerGateway": {key: len(value) for key, value in peer_sets.items()},
        "routedCheckCount": checks, "routeDecision": "peer_direct",
    }, indent=2) + "\n")
    print(output.read_text())


def public_gateway_is_down(url: str, timeout: int = 45) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, body = request(f"{url}/health")
            if status != 200 or json.loads(body).get("service") != "agentgw":
                return True
        except Exception:
            return True
        time.sleep(1)
    return False


def gateway_failover_test(state_dir: pathlib.Path, endpoints_path: pathlib.Path, output: pathlib.Path) -> None:
    runtime_path = state_dir / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    endpoints = read_endpoints(endpoints_path)
    failed_platform = "linux-x64"
    own = next(item for item in endpoints if item["platform"] == failed_platform)
    survivor = next(item for item in endpoints if item["platform"] != failed_platform)
    failed_runtime = next(item for item in runtime["gateways"] if item["platform"] == failed_platform)
    stop_pid(int(failed_runtime["gatewayPid"]))
    if not public_gateway_is_down(own["url"]):
        raise RuntimeError(f"failed gateway remained healthy after owned PID stopped: {own['url']}")
    selected = [peer for peer in wait_for_mabc(survivor["url"]) if peer_platform(peer) == failed_platform]
    if len(selected) != 3:
        raise RuntimeError(f"survivor did not retain the failed host's three Mabc peers: {selected}")
    checks = 0
    for peer in selected:
        status = route(survivor["url"], peer["id"], "admin.status", {})
        if status.get("ok") is not True:
            raise RuntimeError(f"survivor status failed for {peer['id']}")
        checks += 1
    marker = f"AWFAILOVER_{failed_platform}_{os.environ.get('GITHUB_RUN_ID', 'local')}".replace("-", "_")
    checks += verify_exec_and_file(survivor["url"], selected[0], marker)
    gateway_dir = state_dir / failed_platform
    env = json.loads((gateway_dir / "gateway-env.json").read_text(encoding="utf-8"))
    gateway = start_detached([runtime["binary"]], env, gateway_dir / "agentgw.log")
    failed_runtime["gatewayPid"] = gateway.pid
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    wait_json(f"http://127.0.0.1:{failed_runtime['port']}/health", gateway)
    wait_json(own["url"] + "/health", timeout=90)
    recovered = wait_for_mabc(own["url"], timeout=180)
    recovered_gateway_peers = wait_for_gateway_peers(own["url"], failed_platform, timeout=180)
    for peer in recovered:
        status = route(own["url"], peer["id"], "admin.status", {})
        if status.get("ok") is not True:
            raise RuntimeError(f"recovered gateway status failed for {peer['id']}")
        checks += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "ok": True, "failedGateway": failed_platform, "survivingGateway": survivor["platform"],
        "failedGatewayObservedDown": True, "retainedMabcPeerCount": len(selected),
        "recoveredPeerCount": len(recovered), "recoveredGatewayPeerCount": len(recovered_gateway_peers),
        "routedCheckCount": checks, "routeDecision": "peer_direct",
    }, indent=2) + "\n")
    print(output.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("gateway-cluster-start")
    start.add_argument("--state", type=pathlib.Path, required=True)
    start.add_argument("--channel", choices=("main", "prod"), default="prod")
    client = sub.add_parser("client-test")
    client.add_argument("--endpoints", type=pathlib.Path, required=True)
    client.add_argument("--platform", choices=PLATFORMS, required=True)
    client.add_argument("--output", type=pathlib.Path, required=True)
    failover = sub.add_parser("gateway-failover-test")
    failover.add_argument("--state", type=pathlib.Path, required=True)
    failover.add_argument("--endpoints", type=pathlib.Path, required=True)
    failover.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.command == "gateway-cluster-start":
        gateway_cluster_start(args.state, args.channel)
    elif args.command == "client-test":
        client_test(args.endpoints, args.platform, args.output)
    else:
        gateway_failover_test(args.state, args.endpoints, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
