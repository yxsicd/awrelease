#!/usr/bin/env python3
import json
import pathlib

root = pathlib.Path(__file__).resolve().parents[1]
descriptor = json.loads((root / "service.json").read_text())
catalog = json.loads((root / "skills.json").read_text())

assert descriptor["schema"] == "agentweb.release-service.v1"
assert set(descriptor["transports"]) == {"http", "mcp", "websiteSkills"}
assert descriptor["transports"]["mcp"]["compatibility"] == "unchanged"
assert descriptor["releaseChannels"]["default"] == "prod"
assert descriptor["releaseChannels"]["order"] == ["dev", "main", "prod"]
assert descriptor["discovery"]["runtimeEnrollmentDiscoveryPath"] == "/setup/api/bootstrap-info"

for relative in ("README.md", "SKILL.md", "service.json", "skills.json", "install.sh"):
    assert (root / relative).is_file(), relative
for entry in catalog["skills"]:
    skill = (root / entry["path"]).read_text()
    assert skill.startswith("---\n"), entry["path"]
    assert "service-discovery-version: \"1\"" in skill, entry["path"]
    assert "service-manifest:" in skill, entry["path"]

install_skill = (root / "install" / "SKILL.md").read_text()
for required in (
    "/setup/api/bootstrap-info",
    "selfServicePolicyIds",
    "x-agentweb-rgw-token",
    "HTTP 201",
    "oneLine.posix",
):
    assert required in install_skill, required

source_installer = root.parent / "agentweb" / "agentgw" / "src" / "static" / "install.sh"
if source_installer.is_file():
    assert (root / "install.sh").read_bytes() == source_installer.read_bytes(), "installer drift"

workflow = (root / ".github" / "workflows" / "release-smoke.yml").read_text()
assert "--require-runtime-website-skills" in workflow
assert "matrix:\n        channel: [main, prod]" in workflow

print("OK awrelease repository contract")
