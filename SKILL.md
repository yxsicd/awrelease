---
name: agentweb-release
description: Install and discover AgentWeb from its public checksum-verified binary releases.
metadata:
  service-discovery-version: "1"
  service-manifest: "./service.json"
---

# AgentWeb release

Use this repository when a task starts from the AgentWeb GitHub release link and
needs installation, deployment discovery, or the correct public interface.

## Choose the next Skill

- [Install and upgrade](./install/SKILL.md)
- [HTTP API](./http/SKILL.md)
- [Existing MCP service](./mcp/SKILL.md)
- [Website Skills discovery](./website-skills/SKILL.md)
- [Machine-readable catalog](./skills.json)

## First install

Read [Install and upgrade](./install/SKILL.md). The operator must supply the
gateway. The installer uses Basic user `agentweb`, unified verify
`agentwebadmin`, policy `personal-default`, and a production Ma/Mb/Mc profile.
Run:

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --gateway https://gateway.example.com
```

The default channel is `prod`. The installer verifies the manifest-selected
binary before replacing or starting anything. Use `--gateway`, `--device`,
`--policy`, `--profile`, or `--basic` for an explicit deployment. The same
verify is used for RGW HTTP, AWMCP, and Basic claim issuance. A legacy upgraded
gateway may advertise `crc`; custom values must be supplied explicitly.

## Stop conditions

Stop if the claim target, host, channel, platform, checksum, or existing
AgentWeb home differs from the intended deployment. Do not delete an existing
state directory, weaken authentication, invent a gateway URL, or use `dev` as a
production shortcut.
