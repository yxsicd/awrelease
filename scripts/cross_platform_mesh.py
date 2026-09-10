#!/usr/bin/env python3
"""Cross-platform install smoke with a separate RGW and Mabc on every host."""

from __future__ import annotations

import argparse
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
import time
import urllib.error
import urllib.parse
import urllib.request

VERIFY = "agentwebadmin"
PLATFORMS = ("linux-x64", "linux-arm64", "macos-arm64", "windows-x64")
CENTRAL_IDS = ("rgw-a", "rgw-b")
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
    req = urllib.request.Request(url, headers={"User-Agent": "awrelease-mesh/3"})
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
    manifest = get_json(f"https://github.com/yxsicd/awrelease/releases/download/{channel}/agentgw-{channel}.json")
    artifact = manifest["artifacts"][artifact_key()]
    binary = directory / artifact["filename"]
    download(artifact.get("downloadUrl") or artifact["url"], binary)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != artifact["sha256"]:
        raise RuntimeError("AgentGW SHA-256 mismatch")
    if binary.stat().st_size != artifact["size"]:
        raise RuntimeError("AgentGW size mismatch")
    if platform.system() != "Windows":
        binary.chmod(0o755)
    if platform.system() == "Darwin":
        subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return binary


def start_detached(argv: list[str], env: dict[str, str], log: pathlib.Path) -> subprocess.Popen:
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["RUNNER_TRACKING_ID"] = ""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
    return subprocess.Popen(argv, env=child_env, stdout=log.open("ab"), stderr=subprocess.STDOUT,
                            start_new_session=platform.system() != "Windows", creationflags=flags)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def stop_pid(pid: int) -> None:
    if platform.system() == "Windows":
        subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)


def host_gateway_id(platform_name: str) -> str:
    index = PLATFORMS.index(platform_name) + 10
    return f"11111111-1111-4111-8111-{index:012d}"


def host_logical_gateway_id(platform_name: str) -> str:
    return f"gha-host-{platform_name}"


def central_gateway_id(name: str) -> str:
    index = CENTRAL_IDS.index(name) + 1
    return f"22222222-2222-4222-8222-{index:012d}"


def write_host_topology(path: pathlib.Path, platform_name: str, local_url: str) -> None:
    logical_id = host_logical_gateway_id(platform_name)
    topology = {
        "schemaVersion": 2,
        "kind": "agentweb.current-topology",
        "gatewayClusters": [{"id": CLUSTER_ID, "name": "GitHub Actions host-local gateway"}],
        "rgws": [{"id": logical_id, "publicUrl": local_url,
                  "wsUrl": f"{local_url.replace('http://', 'ws://', 1)}/ws?role=upstream"}],
        "registrationPolicies": [{"id": "personal-default", "domainId": "ci",
                                  "gatewayClusterIds": [CLUSTER_ID], "publicGatewayIds": [logical_id]}],
        "enrollmentIssuers": [{"id": "ci", "gatewayInstanceIds": [logical_id],
                               "allowedDomainIds": ["ci"],
                               "allowedRegistrationPolicyIds": ["personal-default"]}],
    }
    path.write_text(json.dumps(topology, indent=2) + "\n", encoding="utf-8")


def gateway_environment(state_dir: pathlib.Path, channel: str, mode: str, port: int,
                        gateway_id: str, logical_id: str, public_url: str,
                        remote_gws: list[str], require_enrollment: bool) -> dict[str, str]:
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    return {
        "AGENTGW_MODE": mode,
        "AGENTGW_BIND": f"127.0.0.1:{port}",
        "AGENTWEB_VERIFY": VERIFY,
        "AGENTGW_RGW_TOKEN": VERIFY,
        "AGENTGW_GATEWAY_ID": gateway_id,
        "AGENTGW_GATEWAY_CLUSTER_ID": CLUSTER_ID,
        "AGENTGW_LOGICAL_GATEWAY_ID": logical_id,
        "AGENTGW_ENROLLMENT_ISSUER_ID": "ci",
        "AGENTGW_ENROLLMENT_SIGNING_KEY": SIGNING_KEY,
        "AGENTGW_REQUIRE_ENROLLMENT_TOKEN": "true" if require_enrollment else "false",
        "AGENTGW_HTTP_UPSTREAM_ENABLED": "1",
        "AGENTGW_SETUP_SELF_SERVICE_POLICY_IDS": "personal-default",
        "AGENTGW_TOPOLOGY_FILE": str(state_dir / "topology.json"),
        "AGENTWEB_RELEASE_CHANNEL": channel,
        "AGENTWEB_PUBLIC_URL": public_url,
        "AGENTGW_DIST_DIR": str(state_dir / "dist"),
        "AGENTGW_NODE_ID_FILE": str(state_dir / "gateway-node-id"),
        "AGENTGW_DEVICE_ID_FILE": str(state_dir / "gateway-device-id"),
        "AGENTGW_DEVICE_NAME": f"gha-{logical_id}-{run_id}",
        "AGENTGW_NODE_NAME": f"gha-{logical_id}-{run_id}",
        "AGENTGW_REMOTE_GWS": ",".join(remote_gws),
        "AGENTGW_UPSTREAM_MODE": "all",
        "AGENTGW_UPSTREAM_TRANSPORT": "auto",
        "AGENTGW_NO_SETUP_BROWSER": "true",
        "AGENTGW_CDP_HEARTBEAT_MS": "0",
    }


def write_haproxy_config(path: pathlib.Path, bind_port: int, backend_ports: dict[str, int]) -> None:
    lines = ["global", "  maxconn 2048", "defaults", "  mode http", "  timeout connect 10s",
             "  timeout client 5m", "  timeout server 5m", "frontend mesh",
             f"  bind 127.0.0.1:{bind_port}"]
    for gateway in CENTRAL_IDS:
        lines.extend([f"  acl path_{gateway.replace('-', '_')} path_beg /{gateway}",
                      f"  use_backend {gateway.replace('-', '_')} if path_{gateway.replace('-', '_')}"])
    for gateway in CENTRAL_IDS:
        safe = gateway.replace("-", "_")
        lines.extend([f"backend {safe}", f"  http-request set-path %[path,regsub(^/{gateway},)]",
                      f"  server {safe} 127.0.0.1:{backend_ports[gateway]}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def start_public_tunnel(proxy_port: int, state_dir: pathlib.Path):
    pattern = re.compile(r"https://[a-z0-9-]+\.free\.pinggy\.net")
    failures = []
    for attempt in range(1, 4):
        log = state_dir / f"pinggy-{attempt}.log"
        process = start_detached([
            "ssh", "-T", "-p", "443", "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=30", "-R", f"0:127.0.0.1:{proxy_port}", "a.pinggy.io",
        ], {}, log)
        deadline = time.monotonic() + 30
        public_url = ""
        while time.monotonic() < deadline and process.poll() is None:
            match = pattern.search(log.read_text(errors="ignore") if log.exists() else "")
            if match:
                public_url = match.group(0)
                host = urllib.parse.urlparse(public_url).hostname
                try:
                    socket.getaddrinfo(host, 443)
                    return process, public_url
                except OSError as error:
                    failures.append(f"attempt {attempt} DNS {host}: {error}")
            time.sleep(2)
        failures.append(f"attempt {attempt}: {log.read_text(errors='ignore')[-500:]}")
        stop_process(process)
    raise RuntimeError("Pinggy HTTPS tunnel failed after three owned attempts: " + " | ".join(failures))


def central_start(state_dir: pathlib.Path, channel: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    binary = download_agentgw(channel, state_dir)
    backend_ports = {name: free_port() for name in CENTRAL_IDS}
    proxy_port = free_port()
    config = state_dir / "haproxy.cfg"
    write_haproxy_config(config, proxy_port, backend_ports)
    subprocess.run(["haproxy", "-c", "-f", str(config)], check=True)
    proxy = start_detached(["haproxy", "-f", str(config), "-db"], {}, state_dir / "haproxy.log")
    tunnel = None
    gateways: dict[str, subprocess.Popen] = {}
    try:
        tunnel, public_url = start_public_tunnel(proxy_port, state_dir)
        endpoints = [{"id": name, "url": f"{public_url}/{name}"} for name in CENTRAL_IDS]
        for endpoint in endpoints:
            name = endpoint["id"]
            directory = state_dir / name
            directory.mkdir()
            (directory / "topology.json").write_text('{"schemaVersion":2,"kind":"agentweb.current-topology","rgws":[]}\n')
            env = gateway_environment(directory, channel, "remote", backend_ports[name], central_gateway_id(name),
                                      name, endpoint["url"], [], False)
            (directory / "gateway-env.json").write_text(json.dumps(env), encoding="utf-8")
            gateways[name] = start_detached([str(binary)], env, directory / "agentgw.log")
            wait_json(f"http://127.0.0.1:{backend_ports[name]}/health", gateways[name])
            wait_json(f"{endpoint['url']}/health", timeout=120)
        bundle = {"channel": channel, "centralGateways": endpoints, "platforms": list(PLATFORMS)}
        (state_dir / "endpoint.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        runtime = {"tunnelPid": tunnel.pid, "proxyPid": proxy.pid, "binary": str(binary),
                   "centralGateways": [{"id": name, "pid": gateways[name].pid, "port": backend_ports[name]}
                                       for name in CENTRAL_IDS]}
        (state_dir / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(bundle))
    except Exception:
        for process in gateways.values():
            stop_process(process)
        if tunnel is not None:
            stop_process(tunnel)
        stop_process(proxy)
        raise


def read_bundle(path: pathlib.Path) -> dict:
    if path.is_dir():
        path = path / "endpoint.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    endpoints = bundle.get("centralGateways", [])
    if [item.get("id") for item in endpoints] != list(CENTRAL_IDS):
        raise RuntimeError(f"endpoint bundle does not contain both central RGWs: {bundle}")
    if not all(str(item.get("url", "")).startswith("https://") for item in endpoints):
        raise RuntimeError(f"endpoint bundle contains a non-HTTPS central RGW: {bundle}")
    return bundle


def edge_start(state_dir: pathlib.Path, endpoints_path: pathlib.Path, platform_name: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    bundle = read_bundle(endpoints_path)
    binary = download_agentgw(bundle["channel"], state_dir)
    port = free_port()
    local_url = f"http://127.0.0.1:{port}"
    write_host_topology(state_dir / "topology.json", platform_name, local_url)
    remote_gws = [f"{item['url'].replace('https://', 'wss://', 1)}/ws?role=upstream"
                  for item in bundle["centralGateways"]]
    env = gateway_environment(state_dir, bundle["channel"], "remote", port, host_gateway_id(platform_name),
                              host_logical_gateway_id(platform_name), local_url, remote_gws, True)
    process = start_detached([str(binary)], env, state_dir / "agentgw.log")
    try:
        health = wait_json(f"{local_url}/health", process)
        result = {"platform": platform_name, "localGateway": local_url, "pid": process.pid,
                  "nodeId": health["nodeId"], "centralGateways": bundle["centralGateways"]}
        (state_dir / "edge.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
    except Exception:
        stop_process(process)
        raise


def peer_platform(peer: dict) -> str | None:
    device = str((peer.get("metadata") or {}).get("deviceName") or peer.get("deviceName") or "")
    for item in PLATFORMS:
        if device.startswith(f"gha-{item}-"):
            return item
    return None


def wait_for_local_mabc(base: str, platform_name: str, timeout: int = 300) -> list[dict]:
    headers = {"x-agentweb-rgw-token": VERIFY}
    deadline = time.monotonic() + timeout
    selected: list[dict] = []
    while time.monotonic() < deadline:
        peers = get_json(f"{base}/api/peers", headers=headers).get("peers", [])
        selected = [peer for peer in peers if peer_platform(peer) == platform_name]
        if len(selected) == 3:
            return selected
        time.sleep(2)
    raise RuntimeError(f"host RGW did not see three managers plus three LGWs: {selected}")


def nested_mabc(base: str) -> tuple[list[dict], list[dict]]:
    headers = {"x-agentweb-rgw-token": VERIFY}
    peers = get_json(f"{base}/api/peers", headers=headers).get("peers", [])
    hosts = [peer for peer in peers if peer.get("role") == "upstream" and
             (peer.get("metadata") or {}).get("kind") == "rgw"]
    nested: dict[str, dict] = {}
    for host in hosts:
        for peer in (host.get("metadata") or {}).get("localPeers") or []:
            if peer_platform(peer) and ":" not in str(peer.get("id") or ""):
                nested[str(peer["id"])] = peer
    return hosts, list(nested.values())


def wait_for_nested_mabc(base: str, expected_count: int, timeout: int = 300) -> tuple[list[dict], list[dict]]:
    deadline = time.monotonic() + timeout
    last = ([], [])
    while time.monotonic() < deadline:
        last = nested_mabc(base)
        platforms = {peer_platform(peer) for peer in last[1]}
        if len(last[0]) == 4 and len(last[1]) == expected_count and platforms == set(PLATFORMS):
            return last
        time.sleep(2)
    raise RuntimeError(f"central RGW topology incomplete: hosts={len(last[0])} nested={len(last[1])}")


def route(base: str, peer: str, name: str, payload: dict, decision: str) -> dict:
    status, _, body = request(f"{base}/api/cmd",
                              {"to": peer, "name": name, "payload": payload, "timeoutMs": 20000},
                              {"Content-Type": "application/json", "x-agentweb-rgw-token": VERIFY})
    value = json.loads(body)
    if status != 200 or value.get("ok") is not True:
        raise RuntimeError(f"{name} to {peer} failed through {base}: HTTP {status} {value}")
    if value.get("targetPeerId") != peer or value.get("routeDecision") != decision:
        raise RuntimeError(f"{name} to {peer} lacked exact route proof through {base}: {value}")
    return value.get("result", {})


def command_payload(marker: str, target_platform: str) -> tuple[str, list[str], str]:
    if target_platform == "windows-x64":
        return "powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", f"[Console]::Write('{marker}')"], \
               f"C:\\Users\\runneradmin\\AppData\\Local\\Temp\\{marker}.txt"
    return "/bin/sh", ["-c", f"printf %s {marker}"], f"/tmp/{marker}.txt"


def verify_exec_and_file(base: str, peer: dict, marker: str, decision: str) -> int:
    peer_id = peer["id"]
    target_platform = peer_platform(peer)
    if target_platform is None:
        raise RuntimeError(f"unknown target platform for {peer_id}")
    command, args, file_path = command_payload(marker, target_platform)
    executed = route(base, peer_id, "admin.system.exec",
                     {"command": command, "args": args, "sync": True, "timeout": 10}, decision)
    record = executed.get("record", {})
    if record.get("exitCode") != 0 or str(record.get("stdout", "")).strip() != marker:
        raise RuntimeError(f"exec mismatch for {peer_id}: {executed}")
    route(base, peer_id, "admin.fs.write", {"path": file_path, "content": marker}, decision)
    read_back = route(base, peer_id, "admin.fs.read", {"path": file_path}, decision)
    if read_back.get("content") != marker:
        raise RuntimeError(f"file mismatch for {peer_id}")
    return 3


def wait_endpoint_down(url: str, timeout: int = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, body = request(f"{url}/health")
            if status != 200 or json.loads(body).get("service") != "agentgw":
                return
        except Exception:
            return
        time.sleep(1)
    raise RuntimeError(f"stopped central RGW remained healthy: {url}")


def client_test(endpoints_root: pathlib.Path, edge_state: pathlib.Path,
                source_platform: str, output: pathlib.Path) -> None:
    bundle = read_bundle(endpoints_root)
    edge = json.loads((edge_state / "edge.json").read_text(encoding="utf-8"))
    local = wait_for_local_mabc(edge["localGateway"], source_platform)
    checks = 0
    for peer in local:
        status = route(edge["localGateway"], peer["id"], "admin.status", {}, "peer_direct")
        if status.get("ok") is not True:
            raise RuntimeError(f"local status failed for {peer['id']}")
        checks += 1
    checks += verify_exec_and_file(edge["localGateway"], local[0],
                                   f"AWLOCAL_{source_platform}_{os.environ.get('GITHUB_RUN_ID', 'local')}".replace("-", "_"),
                                   "peer_direct")
    central_counts = {}
    for endpoint in bundle["centralGateways"]:
        hosts, peers = wait_for_nested_mabc(endpoint["url"], 12)
        central_counts[endpoint["id"]] = {"hostRgws": len(hosts), "mabc": len(peers)}
        own = [peer for peer in peers if peer_platform(peer) == source_platform]
        if len(own) != 3:
            raise RuntimeError(f"{endpoint['id']} did not see this host's three Mabc LGWs")
        for peer in own:
            status = route(endpoint["url"], peer["id"], "admin.status", {}, "upstream_local_peer")
            if status.get("ok") is not True:
                raise RuntimeError(f"central status failed for {peer['id']}")
            checks += 1
        checks += verify_exec_and_file(endpoint["url"], own[0],
                                       f"AWCENTRAL_{endpoint['id']}_{source_platform}_{os.environ.get('GITHUB_RUN_ID', 'local')}".replace("-", "_"),
                                       "upstream_local_peer")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"ok": True, "sourcePlatform": source_platform,
                                  "separateHostRgwPid": edge["pid"], "localMabcCount": len(local),
                                  "centralCounts": central_counts, "routedCheckCount": checks,
                                  "routeDecisions": ["peer_direct", "upstream_local_peer"]}, indent=2) + "\n")
    print(output.read_text())


def gateway_failover_test(state_dir: pathlib.Path, endpoints_path: pathlib.Path, output: pathlib.Path) -> None:
    runtime_path = state_dir / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    bundle = read_bundle(endpoints_path)
    failed = runtime["centralGateways"][0]
    failed_endpoint = bundle["centralGateways"][0]
    survivor = bundle["centralGateways"][1]
    stop_pid(int(failed["pid"]))
    wait_endpoint_down(failed_endpoint["url"])
    hosts, peers = wait_for_nested_mabc(survivor["url"], 12)
    checks = 0
    for peer in peers:
        status = route(survivor["url"], peer["id"], "admin.status", {}, "upstream_local_peer")
        if status.get("ok") is not True:
            raise RuntimeError(f"survivor status failed for {peer['id']}")
        checks += 1
    checks += verify_exec_and_file(survivor["url"], peers[0],
                                   f"AWFAILOVER_{os.environ.get('GITHUB_RUN_ID', 'local')}", "upstream_local_peer")
    directory = state_dir / failed["id"]
    env = json.loads((directory / "gateway-env.json").read_text(encoding="utf-8"))
    process = start_detached([runtime["binary"]], env, directory / "agentgw.log")
    failed["pid"] = process.pid
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    wait_json(f"http://127.0.0.1:{failed['port']}/health", process)
    recovered_hosts, recovered_peers = wait_for_nested_mabc(failed_endpoint["url"], 12)
    for peer in recovered_peers:
        route(failed_endpoint["url"], peer["id"], "admin.status", {}, "upstream_local_peer")
        checks += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"ok": True, "failedCentralRgw": failed["id"],
                                  "survivingCentralRgw": survivor["id"],
                                  "failedEndpointObservedDown": True,
                                  "hostRgwCountDuringFailure": len(hosts),
                                  "mabcCountDuringFailure": len(peers),
                                  "recoveredHostRgwCount": len(recovered_hosts),
                                  "recoveredMabcCount": len(recovered_peers),
                                  "routedCheckCount": checks,
                                  "routeDecision": "upstream_local_peer"}, indent=2) + "\n")
    print(output.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("central-start")
    start.add_argument("--state", type=pathlib.Path, required=True)
    start.add_argument("--channel", choices=("main", "prod"), default="prod")
    edge = sub.add_parser("edge-start")
    edge.add_argument("--state", type=pathlib.Path, required=True)
    edge.add_argument("--endpoints", type=pathlib.Path, required=True)
    edge.add_argument("--platform", choices=PLATFORMS, required=True)
    client = sub.add_parser("client-test")
    client.add_argument("--endpoints", type=pathlib.Path, required=True)
    client.add_argument("--edge-state", type=pathlib.Path, required=True)
    client.add_argument("--platform", choices=PLATFORMS, required=True)
    client.add_argument("--output", type=pathlib.Path, required=True)
    failover = sub.add_parser("gateway-failover-test")
    failover.add_argument("--state", type=pathlib.Path, required=True)
    failover.add_argument("--endpoints", type=pathlib.Path, required=True)
    failover.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.command == "central-start":
        central_start(args.state, args.channel)
    elif args.command == "edge-start":
        edge_start(args.state, args.endpoints, args.platform)
    elif args.command == "client-test":
        client_test(args.endpoints, args.edge_state, args.platform, args.output)
    else:
        gateway_failover_test(args.state, args.endpoints, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
