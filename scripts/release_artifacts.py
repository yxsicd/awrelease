"""Public client/gateway manifest selection, including the bounded migration path."""
from __future__ import annotations

import json
import pathlib
import re
import time
import urllib.error
import urllib.request

CLIENT = {
    "linux-x64-musl": ("agentgw-linux-x64", "x86_64-unknown-linux-musl"),
    "linux-arm64-musl": ("agentgw-linux-arm64", "aarch64-unknown-linux-musl"),
    "macos-arm64": ("agentgw-macos-arm64", "aarch64-apple-darwin"),
    "windows-x64": ("agentgw-windows-x64.exe", "x86_64-pc-windows-gnu"),
}
GATEWAY = {key: (name.replace("agentgw-", "agentgw-server-", 1), target)
           for key, (name, target) in CLIENT.items() if key.startswith("linux-")}


class ManifestMissing(RuntimeError):
    pass


def cache_bust_url(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "cachebust=" + str(time.time_ns())


def load_manifest(service: str, channel: str, repository: str = "yxsicd/awrelease",
                  cache: pathlib.Path | None = None) -> dict:
    name = f"{service}-{channel}.json"
    if cache is not None:
        try:
            return json.loads((cache / name).read_text())
        except FileNotFoundError as exc:
            raise ManifestMissing(name) from exc
    url = cache_bust_url(f"https://github.com/{repository}/releases/download/{channel}/{name}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ManifestMissing(name) from exc
        raise


def validate_manifest(manifest: dict, service: str, channel: str) -> None:
    if (manifest.get("schemaVersion") != 1 or manifest.get("service") != service
            or manifest.get("channel") != channel or manifest.get("buildProfile") != "release"
            or manifest.get("gitDirty") is not False
            or not re.fullmatch(r"[0-9a-f]{12,40}", str(manifest.get("gitSha", "")))):
        raise RuntimeError("invalid release manifest service/channel/source identity")
    if service not in ("agentgw", "agentgw-server"):
        raise RuntimeError("unsupported release service")
    gateway = service == "agentgw-server"
    expected = GATEWAY if gateway else CLIENT
    role = manifest.get("buildRole")
    if (gateway and role != "gateway") or (not gateway and role not in (None, "client")):
        raise RuntimeError("release manifest role mismatch")
    if role is not None:
        features = manifest.get("buildFeatures")
        if not isinstance(features, list) or any(not isinstance(f, str) for f in features):
            raise RuntimeError("release role requires buildFeatures metadata")
        if not gateway and any(f in {"memory-next-gateway", "memory-next-git", "wasmc-compiler", "wasmc-structured-corelib", "rgw-fleet-control", "wasmc-system-telemetry"} or "/" in f for f in features):
            raise RuntimeError("client manifest activates gateway dependencies")
    artifacts = manifest.get("artifacts", {})
    if set(artifacts) != set(expected):
        raise RuntimeError("release manifest requires the complete role artifact set")
    for key, (name, target) in expected.items():
        item = artifacts[key]
        if (item.get("filename") != name or item.get("target") != target
                or item.get("compression", "none") != "none"
                or not isinstance(item.get("size"), int) or item["size"] <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))):
            raise RuntimeError("release artifact role/target/byte metadata mismatch: " + key)


def gateway_manifest(channel: str, repository: str = "yxsicd/awrelease",
                     cache: pathlib.Path | None = None, service: str = "auto") -> dict:
    if service not in ("auto", "agentgw-server", "agentgw"):
        raise RuntimeError("unsupported gateway service")
    if service != "agentgw":
        try:
            manifest = load_manifest("agentgw-server", channel, repository, cache)
        except ManifestMissing:
            if service == "agentgw-server":
                raise
        else:
            validate_manifest(manifest, "agentgw-server", channel)
            return manifest
    # Only a missing server manifest admits the historical combined package.
    # Authentication, transport, JSON, checksum and role errors never fall back.
    manifest = load_manifest("agentgw", channel, repository, cache)
    validate_manifest(manifest, "agentgw", channel)
    if "buildRole" in manifest:
        raise RuntimeError("client release cannot serve as gateway; publish agentgw-server first")
    return manifest
