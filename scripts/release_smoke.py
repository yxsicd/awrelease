#!/usr/bin/env python3
"""Black-box smoke for public AgentWeb HTTP, MCP, and Website Skills releases."""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import json
import os
import pathlib
import platform
import socket
import subprocess
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MCP_VERSION = "2025-06-18"
EXPECTED_TOOLS = {
    "service_metadata",
    "topo",
    "operation_get",
    "skill_list",
    "skill_get",
    "skill_run_read",
    "skill_run_write",
    "skill_run_publish",
}
EXPECTED_PUBLIC_OPERATIONS = {
    "discoverAgentWeb",
    "getApiIndex",
    "getGatewayCapabilities",
    "getLiveTopology",
    "executeCommand",
    "downloadPeerFile",
    "uploadPeerFile",
    "downloadPublication",
    "viewPublicationAsset",
}


def request(
    url: str,
    *,
    payload: dict | None = None,
    data: bytes | None = None,
    headers: dict | None = None,
    method: str | None = None,
    timeout: float = 20,
):
    if payload is not None and data is not None:
        raise ValueError("payload and data are mutually exclusive")
    body = data if payload is None else json.dumps(payload).encode()
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def get_json(url: str, *, headers: dict | None = None) -> dict:
    status, _, body = request(url, headers=headers)
    if status != 200:
        raise RuntimeError(f"GET {url} returned HTTP {status}: {body[:300]!r}")
    return json.loads(body)


def header(headers: dict, name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def expect_json_status(
    url: str,
    expected_status: int,
    *,
    payload: dict | None = None,
    data: bytes | None = None,
    headers: dict | None = None,
    method: str | None = None,
) -> dict:
    status, _, body = request(
        url, payload=payload, data=data, headers=headers, method=method
    )
    if status != expected_status:
        raise RuntimeError(
            f"{method or ('POST' if payload is not None or data is not None else 'GET')} "
            f"{url} returned HTTP {status}, expected {expected_status}: {body[:500]!r}"
        )
    return json.loads(body)


def download(url: str, path: pathlib.Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "awrelease-smoke/1"})
    with urllib.request.urlopen(req, timeout=60) as response, path.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def verify_artifact(manifest: dict, key: str, path: pathlib.Path) -> None:
    artifact = manifest["artifacts"][key]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != artifact["sha256"]:
        raise RuntimeError(f"SHA-256 mismatch for {path.name}")
    if path.stat().st_size != artifact["size"]:
        raise RuntimeError(f"size mismatch for {path.name}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_json(url: str, process: subprocess.Popen, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    last = "not started"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before {url}: rc={process.returncode}")
        try:
            return get_json(url)
        except Exception as error:  # bounded startup polling
            last = str(error)
            time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def wait_peers(url: str, expected: set[str], headers: dict, processes: list[subprocess.Popen], timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    last = set()
    while time.monotonic() < deadline:
        exited = [process.returncode for process in processes if process.poll() is not None]
        if exited:
            raise RuntimeError(f"Mabc exited before registration: {exited}")
        try:
            peers = get_json(url, headers=headers)
            last = {peer.get("id") for peer in peers.get("peers", [])}
            if expected <= last:
                return peers
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for Mabc peers: expected={sorted(expected)} visible={sorted(last)}")


def routed_command(base: str, verify: str, peer: str, name: str, payload: dict) -> dict:
    status, _, body = request(
        urllib.parse.urljoin(base, "api/cmd"),
        payload={"to": peer, "name": name, "payload": payload, "timeoutMs": 15000},
        headers={"Content-Type": "application/json", "x-agentweb-rgw-token": verify},
    )
    if status != 200:
        raise RuntimeError(f"{name} via RGW returned HTTP {status}: {body[:500]!r}")
    value = json.loads(body)
    if value.get("ok") is not True or value.get("targetPeerId") != peer or value.get("routeDecision") != "peer_direct":
        raise RuntimeError(f"{name} did not use exact direct peer {peer}: {value}")
    return value.get("result", {})


def smoke_openapi(openapi: dict) -> dict[str, str]:
    if openapi.get("openapi") != "3.1.0":
        raise RuntimeError("released AgentGW does not expose OpenAPI 3.1")
    operations = {
        operation["operationId"]: f"{method.upper()} {path}"
        for path, methods in openapi.get("paths", {}).items()
        for method, operation in methods.items()
        if isinstance(operation, dict) and "operationId" in operation
    }
    if set(operations) != EXPECTED_PUBLIC_OPERATIONS:
        raise RuntimeError(
            "public OpenAPI operations changed without release-smoke coverage: "
            f"expected={sorted(EXPECTED_PUBLIC_OPERATIONS)} actual={sorted(operations)}"
        )
    schemes = set(openapi.get("components", {}).get("securitySchemes", {}))
    if schemes != {"RgwHeaderToken", "RgwBearerToken"}:
        raise RuntimeError(f"unexpected RGW security schemes: {sorted(schemes)}")
    return operations


def smoke_http_control(base: str, verify: str, peer: str) -> int:
    command_url = urllib.parse.urljoin(base, "api/v1/commands")
    request_payload = {
        "to": peer,
        "name": "admin.status",
        "payload": {},
        "timeoutMs": 15000,
    }
    for headers in ({}, {"x-agentweb-rgw-token": "incorrect"}):
        result = expect_json_status(command_url, 401, payload=request_payload, headers=headers)
        if result.get("ok") is not False:
            raise RuntimeError(f"command authentication failure was not structured: {result}")

    bearer = expect_json_status(
        command_url,
        200,
        payload=request_payload,
        headers={"Authorization": f"Bearer {verify}"},
    )
    if (
        bearer.get("ok") is not True
        or bearer.get("targetPeerId") != peer
        or bearer.get("routeDecision") != "peer_direct"
    ):
        raise RuntimeError(f"Bearer command did not use the exact direct peer: {bearer}")

    missing = expect_json_status(
        command_url,
        200,
        payload={"to": "lgw_missing_release_smoke", "name": "admin.status", "payload": {}},
        headers={"x-agentweb-rgw-token": verify},
    )
    if missing.get("ok") is not False or missing.get("routeDecision") != "peer_missing":
        raise RuntimeError(f"unknown peer did not return peer_missing: {missing}")
    return 4


def smoke_http_file_api(base: str, verify: str, peer: str, path: pathlib.Path) -> dict:
    content = b"\x00AgentWeb-public-file-api\xff\n"
    digest = hashlib.sha256(content).hexdigest()
    query = urllib.parse.urlencode({"to": peer, "path": str(path), "timeoutMs": 15000})
    upload_url = urllib.parse.urljoin(base, f"api/v1/files?{query}")
    unauthorized = expect_json_status(
        upload_url,
        401,
        data=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    if unauthorized.get("ok") is not False:
        raise RuntimeError(f"file upload authentication failure was not structured: {unauthorized}")
    status, upload_headers, upload_body = request(
        upload_url,
        data=content,
        headers={
            "Content-Type": "application/octet-stream",
            "x-agentweb-rgw-token": verify,
            "x-agentweb-sha256": digest,
        },
        timeout=30,
    )
    upload = json.loads(upload_body)
    if status != 200 or upload.get("ok") is not True:
        raise RuntimeError(f"binary upload failed: HTTP {status} {upload}")
    if upload.get("size") != len(content) or upload.get("sha256") != digest:
        raise RuntimeError(f"binary upload identity mismatch: {upload}")

    download_query = urllib.parse.urlencode({"from": peer, "path": str(path), "timeoutMs": 15000})
    download_url = urllib.parse.urljoin(base, f"api/v1/files?{download_query}")
    status, download_headers, downloaded = request(
        download_url, headers={"x-agentweb-rgw-token": verify}, timeout=30
    )
    if status != 200 or downloaded != content:
        raise RuntimeError(f"binary download mismatch: HTTP {status} bytes={len(downloaded)}")
    if header(download_headers, "x-agentweb-sha256") != digest:
        raise RuntimeError("binary download did not return the complete-file SHA-256")
    if header(download_headers, "accept-ranges") != "bytes":
        raise RuntimeError("binary download did not advertise byte ranges")

    status, range_headers, partial = request(
        download_url,
        headers={"x-agentweb-rgw-token": verify, "Range": "bytes=1-8"},
        timeout=30,
    )
    if status != 206 or partial != content[1:9]:
        raise RuntimeError(f"binary range mismatch: HTTP {status} body={partial!r}")
    if header(range_headers, "content-range") != f"bytes 1-8/{len(content)}":
        raise RuntimeError(f"binary range returned an invalid Content-Range: {range_headers}")
    lane = upload.get("dataLane") or header(upload_headers, "x-agentweb-data-lane")
    return {"bytes": len(content), "sha256": digest, "rangeBytes": len(partial), "dataLane": lane}


def decode_mcp(headers: dict, body: bytes) -> dict:
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if "text/event-stream" in content_type or body.lstrip().startswith(b"data:"):
        events = []
        for line in body.decode().splitlines():
            if line.startswith("data:") and line[5:].strip():
                events.append(json.loads(line[5:].strip()))
        if not events:
            raise RuntimeError("MCP SSE response contained no data event")
        return events[-1]
    return json.loads(body)


def mcp_request(url: str, payload: dict) -> dict:
    status, headers, body = request(
        url,
        payload=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_VERSION,
        },
    )
    if status != 200:
        raise RuntimeError(f"MCP request returned HTTP {status}: {body[:500]!r}")
    result = decode_mcp(headers, body)
    if "error" in result:
        raise RuntimeError(f"MCP returned {result['error']}")
    return result["result"]


def mcp_structured(result: dict) -> dict:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                value = json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise RuntimeError(f"MCP tool result has no structured JSON content: {result}")


def mcp_tool(url: str, request_id: int, name: str, arguments: dict) -> tuple[dict, dict]:
    result = mcp_request(
        url,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return result, mcp_structured(result)


def smoke_mcp_tools(url: str, verify: str, peer: str) -> dict:
    calls = []

    def call(name: str, arguments: dict) -> tuple[dict, dict]:
        result, structured = mcp_tool(url, 100 + len(calls), name, arguments)
        calls.append(name)
        if result.get("isError") is True or structured.get("ok") is False:
            raise RuntimeError(f"MCP {name} failed: {result}")
        return result, structured

    call("service_metadata", {"verify": verify})
    _, topology = call("topo", {"verify": verify, "op": "tree"})
    peer_prefix = peer.removeprefix("lgw_")[:5]
    if peer_prefix not in json.dumps(topology) and peer not in json.dumps(topology):
        raise RuntimeError("MCP topology did not expose the release-smoke peer")
    _, skills = call("skill_list", {"verify": verify})
    skill_ids = {item.get("id") for item in skills.get("skills", [])}
    if not {"gateway-observation", "host-development"} <= skill_ids:
        raise RuntimeError(f"MCP built-in skills are incomplete: {sorted(skill_ids)}")
    call("skill_get", {"verify": verify, "skill": "host-development"})
    _, status = call(
        "skill_run_read",
        {
            "verify": verify,
            "skill": "host-development",
            "action": "a_status",
            "input": {"peerId": peer},
        },
    )
    if status.get("targetPeerId") != peer or status.get("routeDecision") != "peer_direct":
        raise RuntimeError(f"MCP read did not prove the exact direct target: {status}")
    marker = "AWMCP_RELEASE_WRITE"
    _, executed = call(
        "skill_run_write",
        {
            "verify": verify,
            "skill": "host-development",
            "action": "a_exec",
            "input": {
                "peerId": peer,
                "command": "/bin/sh",
                "args": ["-c", f"printf %s {marker}"],
                "sync": True,
                "timeout": 5,
            },
        },
    )
    record = executed.get("result", {}).get("record", {})
    if executed.get("targetPeerId") != peer or record.get("stdout") != marker:
        raise RuntimeError(f"MCP write did not execute on the exact target: {executed}")

    invalid_result, invalid = mcp_tool(url, 199, "service_metadata", {"verify": "incorrect"})
    if invalid_result.get("isError") is not True or invalid.get("ok") is not False:
        raise RuntimeError(f"MCP invalid verify was accepted: {invalid_result}")
    return {"calls": calls, "callCount": len(calls), "invalidVerifyRejected": True}


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


@contextlib.contextmanager
def website_server(root: pathlib.Path):
    handler = functools.partial(QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def smoke_website_skills(root: pathlib.Path) -> None:
    with website_server(root) as base:
        status, _, body = request(urllib.parse.urljoin(base, "SKILL.md"))
        skill = body.decode()
        if status != 200 or not skill.startswith("---\n"):
            raise RuntimeError("root Website Skill is unavailable or malformed")
        descriptor = get_json(urllib.parse.urljoin(base, "service.json"))
        catalog = get_json(urllib.parse.urljoin(base, descriptor["discovery"]["catalog"]))
        if set(descriptor["transports"]) != {"http", "mcp", "websiteSkills"}:
            raise RuntimeError("release descriptor does not expose three surfaces")
        for item in catalog["skills"]:
            status, _, _ = request(urllib.parse.urljoin(base, item["path"]))
            if status != 200:
                raise RuntimeError(f"Website Skill is unavailable: {item['path']}")


def smoke_runtime_website_skills(base: str, discovery: dict) -> None:
    descriptor = get_json(urllib.parse.urljoin(base, "service.json"))
    catalog = get_json(urllib.parse.urljoin(base, descriptor["discovery"]["catalog"]))
    if descriptor.get("schema") != "agentweb.service-interfaces.v1":
        raise RuntimeError("released AgentGW has an unexpected service descriptor")
    if set(descriptor.get("transports", {})) != {"http", "mcp", "websiteSkills"}:
        raise RuntimeError("released AgentGW does not expose three surfaces")
    if descriptor["transports"]["mcp"].get("compatibility") != "unchanged":
        raise RuntimeError("released AgentGW changed the declared MCP compatibility boundary")
    if discovery.get("interfaces", {}).get("websiteSkills") != urllib.parse.urljoin(base, "SKILL.md"):
        raise RuntimeError("AgentGW discovery does not advertise its root Website Skill")
    for item in catalog.get("skills", []):
        status, _, body = request(urllib.parse.urljoin(base, item["path"]))
        if status != 200 or not body.startswith(b"---\n"):
            raise RuntimeError(f"runtime Website Skill is unavailable: {item['path']}")


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=("dev", "main", "prod"), default="prod")
    parser.add_argument("--repository", default="yxsicd/awrelease")
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-runtime-website-skills", action="store_true")
    parser.add_argument(
        "--asset-cache",
        type=pathlib.Path,
        help="Use pre-fetched public manifests and binaries from this directory",
    )
    args = parser.parse_args()

    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SystemExit("release smoke currently requires Linux amd64")

    root = pathlib.Path(__file__).resolve().parents[1]
    base = f"https://github.com/{args.repository}/releases/download/{args.channel}"
    with tempfile.TemporaryDirectory(prefix="awrelease-smoke-") as temp:
        temp_path = pathlib.Path(temp)
        manifests = {}
        binaries = {}
        for service in ("agentgw", "awmcp"):
            manifest_name = f"{service}-{args.channel}.json"
            if args.asset_cache:
                manifest = json.loads((args.asset_cache / manifest_name).read_text())
            else:
                manifest = get_json(f"{base}/{manifest_name}")
            if manifest.get("channel") != args.channel or manifest.get("gitDirty") is not False:
                raise RuntimeError(f"invalid {service} channel manifest identity")
            artifact = manifest["artifacts"]["linux-x64-musl"]
            binary = temp_path / artifact["filename"]
            if args.asset_cache:
                shutil.copy2(args.asset_cache / artifact["filename"], binary)
            else:
                download(artifact.get("downloadUrl") or artifact["url"], binary)
            verify_artifact(manifest, "linux-x64-musl", binary)
            binary.chmod(0o755)
            manifests[service] = manifest
            binaries[service] = binary

        gateway_port, mcp_port = free_port(), free_port()
        verify = "agentwebadmin"
        role_ids = {
            "ma": "lgw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "mb": "lgw_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "mc": "lgw_cccccccccccccccccccccccccccccccc",
        }
        gateway_log = (temp_path / "agentgw.log").open("wb")
        mcp_log = (temp_path / "awmcp.log").open("wb")
        manager_logs = []
        managers = []
        gateway = mcp = None
        try:
            gateway_env = os.environ.copy()
            gateway_env.update({
                "AGENTGW_MODE": "remote",
                "AGENTGW_BIND": f"127.0.0.1:{gateway_port}",
                "AGENTWEB_VERIFY": verify,
                "AGENTWEB_PUBLIC_URL": f"http://127.0.0.1:{gateway_port}",
                "AGENTWEB_PUBLIC_MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp",
            })
            gateway = subprocess.Popen(
                [str(binaries["agentgw"])], env=gateway_env,
                stdout=gateway_log, stderr=subprocess.STDOUT,
            )
            gateway_health = wait_json(f"http://127.0.0.1:{gateway_port}/health", gateway)
            build_info = get_json(f"http://127.0.0.1:{gateway_port}/build-info")
            gateway_base = f"http://127.0.0.1:{gateway_port}/"
            discovery = get_json(urllib.parse.urljoin(gateway_base, ".well-known/agentweb"))
            api_index = get_json(urllib.parse.urljoin(gateway_base, "api/v1"))
            capabilities = get_json(urllib.parse.urljoin(gateway_base, "api/v1/capabilities"))
            topology = get_json(urllib.parse.urljoin(gateway_base, "api/v1/topology?view=basic"))
            auth_headers = {"x-agentweb-rgw-token": verify}
            openapi = get_json(urllib.parse.urljoin(gateway_base, "api/v1/openapi.json"))
            openapi_operations = smoke_openapi(openapi)
            if gateway_health.get("service") != "agentgw" or build_info["build"]["gitDirty"]:
                raise RuntimeError("released AgentGW identity is not clean")
            if api_index.get("service") != "agentgw" or capabilities.get("ok") is not True:
                raise RuntimeError("released AgentGW API index or capabilities are invalid")
            if topology.get("ok") is not True or not isinstance(topology.get("peers"), list):
                raise RuntimeError("released AgentGW public topology is invalid")

            for role, node_id in role_ids.items():
                role_dir = temp_path / role
                role_dir.mkdir()
                manager_log = (temp_path / f"agentgw-{role}.log").open("wb")
                manager_logs.append(manager_log)
                manager_env = os.environ.copy()
                manager_env.update({
                    "AGENTGW_MODE": "manager",
                    "AGENTGW_LISTEN": "none",
                    "AGENTGW_BIND": "127.0.0.1:17888",
                    "AGENTGW_NODE_ID": node_id,
                    "AGENTGW_DEVICE_ID": "dev_awrelease_smoke",
                    "AGENTGW_NODE_NAME": f"awrelease-smoke-{role}",
                    "AGENTGW_DEVICE_NAME": "awrelease-smoke",
                    "AGENTGW_MANAGER_ROLE": role,
                    "AGENTGW_REMOTE_GWS": f"ws://127.0.0.1:{gateway_port}/ws?role=upstream",
                    "AGENTGW_UPSTREAM_MODE": "all",
                    "AGENTGW_CDP_HTTP": "http://127.0.0.1:9",
                    "AGENTGW_CDP_HEARTBEAT_MS": "0",
                    "AGENTGW_MANAGER_DIR": str(role_dir / "manager"),
                    "AGENTGW_MANAGER_CHILD_ENV_FILES": "",
                    "AGENTGW_DIST_DIR": str(role_dir / "dist"),
                })
                managers.append(subprocess.Popen(
                    [str(binaries["agentgw"])], env=manager_env,
                    stdout=manager_log, stderr=subprocess.STDOUT,
                ))

            wait_peers(
                f"http://127.0.0.1:{gateway_port}/api/peers",
                set(role_ids.values()), auth_headers, managers,
            )
            http_control_checks = smoke_http_control(gateway_base, verify, role_ids["ma"])
            file_api = smoke_http_file_api(
                gateway_base, verify, role_ids["ma"], temp_path / "binary-api-roundtrip.bin"
            )
            publication_error = expect_json_status(
                urllib.parse.urljoin(gateway_base, "api/v1/publications/release-smoke/download"),
                503,
            )
            if publication_error.get("ok") is not False or not isinstance(publication_error.get("error"), dict):
                raise RuntimeError(f"publication registry failure was not structured: {publication_error}")
            routed_checks = 0
            for role, peer in role_ids.items():
                status = routed_command(
                    f"http://127.0.0.1:{gateway_port}/", verify, peer, "admin.status", {}
                )
                if status.get("ok") is not True:
                    raise RuntimeError(f"{role} admin.status failed: {status}")
                routed_checks += 1
                marker = f"AWRELEASE_{args.channel}_{role}"
                executed = routed_command(
                    f"http://127.0.0.1:{gateway_port}/", verify, peer,
                    "admin.system.exec",
                    {"command": "/bin/sh", "args": ["-c", f"printf %s {marker}"], "sync": True, "timeout": 5},
                )
                record = executed.get("record", {})
                if record.get("exitCode") != 0 or record.get("stdout") != marker:
                    raise RuntimeError(f"{role} routed exec failed: {executed}")
                routed_checks += 1
                test_file = str(temp_path / f"{role}-roundtrip.txt")
                written = routed_command(
                    f"http://127.0.0.1:{gateway_port}/", verify, peer,
                    "admin.fs.write", {"path": test_file, "content": marker},
                )
                if written.get("ok") is not True:
                    raise RuntimeError(f"{role} routed file write failed: {written}")
                routed_checks += 1
                read_back = routed_command(
                    f"http://127.0.0.1:{gateway_port}/", verify, peer,
                    "admin.fs.read", {"path": test_file},
                )
                if read_back.get("ok") is not True or read_back.get("content") != marker:
                    raise RuntimeError(f"{role} routed file read failed: {read_back}")
                routed_checks += 1
                children = routed_command(
                    f"http://127.0.0.1:{gateway_port}/", verify, peer,
                    "manager.child.list", {},
                )
                if children.get("ok") is not True or children.get("count") != 0:
                    raise RuntimeError(f"{role} manager child list failed: {children}")
                routed_checks += 1

            mcp_env = os.environ.copy()
            mcp_env.update({
                "AWMCP_BIND": f"127.0.0.1:{mcp_port}",
                "AWMCP_RGWS": f"http://127.0.0.1:{gateway_port}",
                "AWMCP_PUBLIC_GATEWAYS": "",
                "AGENTWEB_VERIFY": verify,
                "AWMCP_PLAYWRIGHT_ENABLED": "false",
                "AWMCP_SKILL_REPO_DIR": str(temp_path / "skills"),
            })
            mcp = subprocess.Popen(
                [str(binaries["awmcp"])], env=mcp_env,
                stdout=mcp_log, stderr=subprocess.STDOUT,
            )
            wait_json(f"http://127.0.0.1:{mcp_port}/health", mcp)
            mcp_ready = get_json(f"http://127.0.0.1:{mcp_port}/health/ready")
            mcp_build = get_json(f"http://127.0.0.1:{mcp_port}/build-info")
            mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
            initialized = mcp_request(mcp_url, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": MCP_VERSION, "capabilities": {},
                           "clientInfo": {"name": "awrelease-smoke", "version": "1"}},
            })
            tools = mcp_request(mcp_url, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
            })["tools"]
            names = {tool["name"] for tool in tools}
            if names != EXPECTED_TOOLS:
                raise RuntimeError(f"AWMCP kernel changed: {sorted(names)}")
            if initialized.get("protocolVersion") != MCP_VERSION:
                raise RuntimeError("AWMCP negotiated an unexpected protocol version")
            if mcp_ready.get("ready") is not True or mcp_build.get("build", {}).get("gitDirty") is not False:
                raise RuntimeError("released AWMCP readiness or build identity is invalid")
            mcp_evidence = smoke_mcp_tools(mcp_url, verify, role_ids["ma"])

            smoke_website_skills(root)
            if args.require_runtime_website_skills:
                smoke_runtime_website_skills(
                    f"http://127.0.0.1:{gateway_port}/", discovery
                )
            report = {
                "schema": "agentweb.release-smoke.v1",
                "ok": True,
                "channel": args.channel,
                "surfaces": ["http", "mcp", "websiteSkills"],
                "businessMcpToolsInvoked": True,
                "mcpToolCount": len(tools),
                "mcpToolCalls": mcp_evidence,
                "agentgwGitSha": manifests["agentgw"]["gitSha"],
                "awmcpGitSha": manifests["awmcp"]["gitSha"],
                "runtimeMcpAdvertised": discovery["interfaces"].get("mcp"),
                "runtimeWebsiteSkillsVerified": args.require_runtime_website_skills,
                "mabcPeers": role_ids,
                "mabcRoutedCommandCount": routed_checks,
                "mabcFileRoundTrips": len(role_ids),
                "httpControlChecks": http_control_checks,
                "httpFileApi": file_api,
                "openapiOperations": openapi_operations,
                "publicationRegistryUnavailableRejected": True,
            }
            output = pathlib.Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(json.dumps(report, indent=2, sort_keys=True))
        finally:
            stop(mcp)
            for manager in managers:
                stop(manager)
            stop(gateway)
            mcp_log.close()
            for manager_log in manager_logs:
                manager_log.close()
            gateway_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
