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

Obtain a single-use enrollment claim from the authorized target gateway, then:

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --enroll '<claim-url>'
```

The default channel is `prod`. The installer verifies the manifest-selected
binary before replacing or starting anything. An enrollment claim, credentials,
deployment host, and peer identity are authority supplied by the operator; this
Skill cannot create or infer them.

## Stop conditions

Stop if the claim target, host, channel, platform, checksum, or existing
AgentWeb home differs from the intended deployment. Do not delete an existing
state directory, weaken authentication, invent a gateway URL, or use `dev` as a
production shortcut.
