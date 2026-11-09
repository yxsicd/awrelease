#!/usr/bin/env python3
"""Offline POSIX installer package-reuse contract; never starts system services."""

import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "install.sh").read_text()


def function(name):
    marker = f"\n{name}() {{\n"
    index = SOURCE.find(marker)
    if index < 0:
        raise AssertionError(f"missing production installer function {name}")
    start = index + 1
    end = SOURCE.find("\n}\n", start)
    if end < 0:
        raise AssertionError(f"cannot locate end of {name}")
    return SOURCE[start : end + 2]


FUNCTIONS = "\n".join(
    function(name)
    for name in (
        "log",
        "fail",
        "download",
        "download_resumable",
        "sha256_file",
        "json_value_for_artifact",
        "matches_release_sha",
        "new_runtime_id",
        "prepare_release_artifact",
        "write_env",
    )
)

SCRIPT = (
    "set -eu\n"
    + FUNCTIONS
    + """
install_root="$1"
bin_path="$2"
manifest_url="$3"
expected_digest="$4"
with_roles="$5"
tmp_dir="${install_root}/tmp"
manifest_path="${tmp_dir}/main.json"
artifact_key="macos-arm64"
expected_target="aarch64-apple-darwin"
prepared_bin=""
prepared_sha=""
prepared_manifest_url=""
mkdir -p "$tmp_dir"
prepare_release_artifact "$manifest_url"
[ "$prepared_sha" = "$expected_digest" ]
[ -f "$prepared_bin" ]
[ "$(sha256_file "$prepared_bin")" = "$expected_digest" ]
printf 'PREPARED:%s\\n' "$prepared_bin"

if [ "$with_roles" = "yes" ]; then
  agentweb_home="$install_root"
  env_dir="${install_root}/env.d"
  state_dir="${install_root}/state"
  mkdir -p "$env_dir" "$state_dir"
  gateway_verify="test-local-value"
  device_name="offline_test"
  remote_gws="wss://invalid.example/ws?role=upstream"
  upstream_transport="auto"
  upstream_proxy=""
  management_domain_id="offline-test"
  registration_policy_id="test"
  install_profile="production"
  for role in ma mb mc; do
    write_env "$role" manager none none test-browser "$role"
  done
fi
"""
)


def exercise(root, payload, *, bin_bytes=None, cache_bytes=None, legacy_bytes=None,
             partial_bytes=None, downloadable=False, roles=False, should_pass=True):
    root.mkdir(parents=True)
    home = root / "home"
    bin_path = home / "bin" / "agentgw"
    bin_path.parent.mkdir(parents=True)
    tmp = home / "tmp"
    tmp.mkdir(parents=True)
    cache = tmp / "release-cache"
    cache.mkdir()
    digest = hashlib.sha256(payload).hexdigest()
    asset = root / "source-package"
    asset.write_bytes(payload)
    url = asset.as_uri() if downloadable else (root / "no-such-asset").as_uri()
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"artifacts": {"macos-arm64": {
            "downloadUrl": url,
            "sha256": digest,
            "target": "aarch64-apple-darwin",
        }}})
    )
    cache_path = cache / f"agentgw.macos-arm64.{digest}"
    if bin_bytes is not None:
        bin_path.write_bytes(bin_bytes)
    if cache_bytes is not None:
        cache_path.write_bytes(cache_bytes)
    if legacy_bytes is not None:
        (tmp / "agentgw.macos-arm64.123456").write_bytes(legacy_bytes)
    if partial_bytes is not None:
        pathlib.Path(str(cache_path) + ".part").write_bytes(partial_bytes)
    result = subprocess.run(
        ["/bin/sh", "-c", SCRIPT, "installer-test", str(home),
         str(bin_path), manifest.as_uri(), digest, "yes" if roles else "no"],
        capture_output=True, text=True, timeout=15,
    )
    if not should_pass:
        assert result.returncode != 0, result.stdout
        return
    assert result.returncode == 0, (result.stdout, result.stderr)
    prepared = next(
        line[len("PREPARED:"):] for line in result.stdout.splitlines()
        if line.startswith("PREPARED:")
    )
    assert pathlib.Path(prepared).read_bytes() == payload, result.stderr
    if roles:
        files = [home / role / "bin" / "agentgw" for role in ("ma", "mb", "mc")]
        assert all(path.read_bytes() == payload for path in files)
        assert len({path.stat().st_ino for path in files}) == 3
    return bin_path, cache_path, pathlib.Path(prepared), result.stderr


def main():
    payload = b"local signed-release binary fixture" * 100
    with tempfile.TemporaryDirectory(prefix="awrelease-cache-test-") as d:
        root = pathlib.Path(d)
        bin_path, cache, prepared, logs = exercise(
            root / "local-bin", payload,
            bin_bytes=payload, roles=True)
        assert prepared == bin_path
        assert not cache.exists()
        assert "reusing verified" in logs

        _, cache, _, logs = exercise(
            root / "cached", payload,
            bin_bytes=b"outdated", cache_bytes=payload)
        assert cache.read_bytes() == payload
        assert "package cache: reusing verified" in logs

        _, cache, _, logs = exercise(
            root / "legacy", payload, legacy_bytes=payload)
        assert cache.read_bytes() == payload
        assert "reused verified legacy tmp package" in logs

        _, cache, _, _ = exercise(
            root / "first-download", payload, downloadable=True)
        assert cache.read_bytes() == payload

        _, cache, _, _ = exercise(
            root / "partial-resume", payload,
            partial_bytes=payload[: len(payload) // 2],
            downloadable=True)
        assert cache.read_bytes() == payload

        exercise(root / "missing", payload, should_pass=False)

    print("PASS: bin priority, independent Ma/Mb/Mc copies, cache, legacy tmp, partial resume, no-asset failure")


class InstallerCacheTests(unittest.TestCase):
    def test_offline_reuse_and_role_copies(self):
        main()


if __name__ == "__main__":
    main()
