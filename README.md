# AgentWeb public releases

This repository is the public install and binary release authority for
[AgentWeb](https://github.com/yxsicd/agentweb). An agent can start from this
single GitHub URL, read [`SKILL.md`](SKILL.md), select the required capability,
and install only checksum-verified release bytes.

## Install an AgentWeb node

The gateway must be supplied by the deployment operator. New deployments use
the unified verify `agentwebadmin`; the fixed Basic username is `agentweb`.
The installer requests and consumes its own single-use enrollment claim:

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --gateway https://gateway.example.com
```

The installer detects Linux amd64/arm64 or macOS arm64, downloads the
service-qualified `agentgw-prod.json` manifest, verifies the selected binary's
SHA-256, writes the AgentWeb manager profile, starts it with systemd or launchd,
and checks local `/build-info` readiness.

Windows x64 uses the matching public PowerShell installer:

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/yxsicd/awrelease/main/install.ps1'))) `
  -Gateway 'https://gateway.example.com'
```

It performs the same claim, manifest, SHA-256, manager-profile, persistence, and
readiness checks using LIMITED scheduled tasks with a current-user Startup
shortcut fallback.

Use `--gateway`, `--device`, `--policy`, `--profile`, or `--basic` when the
deployment differs. The verify is one value shared by RGW HTTP, AWMCP tool
calls, and Basic claim issuance. An upgraded gateway may advertise and retain
the historical `crc` default; explicit existing configuration always wins.

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
or credentials. It verifies manifests and hashes, starts RGW plus same-host
Ma/Mb/Mc, routes status, command execution, file round trips, and manager
inspection through RGW to every exact peer, then checks AWMCP read-only
`initialize`/`tools/list` and both Website Skills surfaces. It never invokes an
MCP business tool.

The cross-platform install mesh additionally runs the real public installer on
Linux x64, Linux arm64, macOS arm64, and Windows x64. Each native host starts a
separate local RGW process, then installs Ma/Mb/Mc through that RGW. Every host
RGW connects outbound to two independent central RGWs behind one public tunnel.
The gate proves local `peer_direct` routing and central
`upstream_local_peer` routing to each exact Ma/Mb/Mc manager and its LGW,
including synchronous
command and file round trips. It then stops one central RGW, proves all four
host RGWs and all twenty-four manager/LGW peers remain reachable through the survivor,
restarts the failed RGW, and requires the full peer set again. The tunnel,
topology, and enrollment signing key exist only for that workflow run; no
private deployment endpoint or credential is used.

Run the same black-box check locally on Linux amd64:

```sh
python3 scripts/release_smoke.py --channel prod --output /tmp/agentweb-release-smoke.json
```

Source code and environment-specific deployment state remain in their owning
repositories and hosts. This repository stores public onboarding material and
release bytes only.
