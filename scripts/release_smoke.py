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


def request(url: str, *, payload: dict | None = None, headers: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def get_json(url: str) -> dict:
    status, _, body = request(url)
    if status != 200:
        raise RuntimeError(f"GET {url} returned HTTP {status}: {body[:300]!r}")
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
            binary = (
                args.asset_cache / artifact["filename"]
                if args.asset_cache
                else temp_path / artifact["filename"]
            )
            if not args.asset_cache:
                download(artifact.get("downloadUrl") or artifact["url"], binary)
            verify_artifact(manifest, "linux-x64-musl", binary)
            binary.chmod(0o755)
            manifests[service] = manifest
            binaries[service] = binary

        gateway_port, mcp_port = free_port(), free_port()
        gateway_log = (temp_path / "agentgw.log").open("wb")
        mcp_log = (temp_path / "awmcp.log").open("wb")
        gateway = mcp = None
        try:
            gateway_env = os.environ.copy()
            gateway_env.update({
                "AGENTGW_MODE": "remote",
                "AGENTGW_BIND": f"127.0.0.1:{gateway_port}",
                "AGENTWEB_PUBLIC_URL": f"http://127.0.0.1:{gateway_port}",
                "AGENTWEB_PUBLIC_MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp",
            })
            gateway = subprocess.Popen(
                [str(binaries["agentgw"])], env=gateway_env,
                stdout=gateway_log, stderr=subprocess.STDOUT,
            )
            gateway_health = wait_json(f"http://127.0.0.1:{gateway_port}/health", gateway)
            build_info = get_json(f"http://127.0.0.1:{gateway_port}/build-info")
            discovery = get_json(f"http://127.0.0.1:{gateway_port}/.well-known/agentweb")
            get_json(f"http://127.0.0.1:{gateway_port}/api/v1/openapi.json")
            if gateway_health.get("service") != "agentgw" or build_info["build"]["gitDirty"]:
                raise RuntimeError("released AgentGW identity is not clean")

            mcp_env = os.environ.copy()
            mcp_env.update({
                "AWMCP_BIND": f"127.0.0.1:{mcp_port}",
                "AWMCP_RGWS": f"http://127.0.0.1:{gateway_port}",
                "AWMCP_PUBLIC_GATEWAYS": "",
                "AWMCP_PLAYWRIGHT_ENABLED": "false",
                "AWMCP_SKILL_REPO_DIR": str(temp_path / "skills"),
            })
            mcp = subprocess.Popen(
                [str(binaries["awmcp"])], env=mcp_env,
                stdout=mcp_log, stderr=subprocess.STDOUT,
            )
            wait_json(f"http://127.0.0.1:{mcp_port}/health", mcp)
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
                "businessMcpToolsInvoked": False,
                "mcpToolCount": len(tools),
                "agentgwGitSha": manifests["agentgw"]["gitSha"],
                "awmcpGitSha": manifests["awmcp"]["gitSha"],
                "runtimeMcpAdvertised": discovery["interfaces"].get("mcp"),
                "runtimeWebsiteSkillsVerified": args.require_runtime_website_skills,
            }
            output = pathlib.Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(json.dumps(report, indent=2, sort_keys=True))
        finally:
            stop(mcp)
            stop(gateway)
            mcp_log.close()
            gateway_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
