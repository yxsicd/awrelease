#!/usr/bin/env python3
"""Small GitHub Actions artifact rendezvous helper for concurrent mesh jobs."""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
import zipfile


def api(path: str) -> dict:
    token = os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "awrelease-mesh/1",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def artifacts() -> list[dict]:
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    return api(f"/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]


def wait_for(predicate, timeout: int) -> list[dict]:
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        try:
            last = artifacts()
            if predicate(last):
                return last
        except (urllib.error.URLError, KeyError):
            pass
        time.sleep(5)
    raise RuntimeError(f"artifact rendezvous timed out; visible={[a.get('name') for a in last]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("wait-download")
    download.add_argument("--name", required=True)
    download.add_argument("--out", type=pathlib.Path, required=True)
    download.add_argument("--timeout", type=int, default=900)
    count = sub.add_parser("wait-count")
    count.add_argument("--prefix", required=True)
    count.add_argument("--count", type=int, required=True)
    count.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    if args.command == "wait-download":
        items = wait_for(lambda xs: any(x["name"] == args.name for x in xs), args.timeout)
        artifact = next(x for x in items if x["name"] == args.name)
        req = urllib.request.Request(
            artifact["archive_download_url"],
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "User-Agent": "awrelease-mesh/1"},
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
            args.out.mkdir(parents=True, exist_ok=True)
            archive.extractall(args.out)
        return 0

    items = wait_for(
        lambda xs: sum(1 for x in xs if x["name"].startswith(args.prefix)) >= args.count,
        args.timeout,
    )
    names = sorted(x["name"] for x in items if x["name"].startswith(args.prefix))
    print(json.dumps({"ok": True, "artifacts": names}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
