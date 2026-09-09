# AgentWeb public releases

This repository is the public install and binary release authority for
[AgentWeb](https://github.com/yxsicd/agentweb). An agent can start from this
single GitHub URL, read [`SKILL.md`](SKILL.md), select the required capability,
and install only checksum-verified release bytes.

## Install an enrolled AgentWeb node

Given an operator-approved gateway, first read its public
`/setup/api/bootstrap-info`. The linked [installation Skill](install/SKILL.md)
explains self-service and management-authorized claim issuance. The gateway
returns a single-use platform command; its POSIX form is:

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --enroll 'https://<gateway>/setup/api/enrollment/manifest/<claim>'
```

The installer detects Linux amd64/arm64 or macOS arm64, downloads the
service-qualified `agentgw-prod.json` manifest, verifies the selected binary's
SHA-256, writes the AgentWeb manager profile, starts it with systemd or launchd,
and checks local `/build-info` readiness.

The release repository contains no deployment token. A non-self-service policy
requires the gateway's management credential in an HTTP header when creating
the claim; the installer then handles the returned enrollment credentials
without exposing them to the Agent.

Use `--channel main` only for an operator-owned canary. Use `dev` only for a
release-engineering canary. The immutable artifact set is built once and
promoted byte-for-byte through `dev -> main -> prod`.

## Agent discovery after deployment

AgentWeb exposes three complementary surfaces:

| Surface | Entry | Use |
| --- | --- | --- |
| HTTP | `https://<gateway>/.well-known/agentweb` | Discover the gateway, OpenAPI, topology, commands, files, and publications |
| MCP | the deployment's advertised AWMCP `/mcp` URL | Discover the stable eight-tool Agent Kernel and run selected Skills |
| Website Skills | `https://<gateway>/SKILL.md` | Read task guidance and progressively linked capability Skills |

AgentGW owns HTTP and Website Skills. AWMCP remains a separate service and its
existing transport, tool, verification, and response contracts are unchanged.
Never guess an MCP URL from the gateway hostname; read the runtime descriptor or
ask the operator when the deployment has not advertised one.

## Release assets

Fixed GitHub Release tags are `dev`, `main`, and `prod`. Each channel contains
service-qualified manifests and platform binaries for AgentGW, AWMCP, and
AWGDrive. Consumers must use `agentgw-<channel>.json`,
`awmcp-<channel>.json`, or `awgdrive-<channel>.json` and verify the declared
size and SHA-256 before execution.

Examples:

```text
https://github.com/yxsicd/awrelease/releases/download/prod/agentgw-prod.json
https://github.com/yxsicd/awrelease/releases/download/prod/awmcp-prod.json
```

## Public release smoke

GitHub Actions runs the public `main` and `prod` binaries without private source
or credentials. It verifies manifests and hashes, starts AgentGW and AWMCP,
checks AgentGW HTTP discovery, performs read-only MCP `initialize` and
`tools/list`, follows the release-repository Skills, and requires the released
AgentGW's runtime Website Skills and three-surface descriptor. It never invokes
a business MCP tool.

Run the same black-box check locally on Linux amd64:

```sh
python3 scripts/release_smoke.py --channel prod --output /tmp/agentweb-release-smoke.json
```

Source code and environment-specific deployment state remain in their owning
repositories and hosts. This repository stores public onboarding material and
release bytes only.
