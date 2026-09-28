---
name: agentweb-install
description: Install an enrolled AgentWeb manager profile from verified public release assets.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# Install AgentWeb

This repository owns both public installer scripts. Gateways should emit the
raw `awrelease/main` installer URLs and keep any gateway-local `/install.sh` or
`/install.ps1` route only as a compatibility redirect. A script-only correction
is released by updating this repository; never rebuild AgentGW merely to change
installer logic. The Windows release target is the manifest-authoritative
`x86_64-pc-windows-gnu` target produced by the public release workflow.
Windows repair must never overwrite a running shared `bin/agentgw.exe` in
place. Materialize a SHA-verified, versioned binary independently under every
role directory, point that role's supervisor and `AGENTGW_SELF_PATH` at its own
copy, stop only the process associated with that exact role config, then
activate in Mb, Mc, Ma order. The historical shared path is compatibility-only.

Prerequisites are `curl` or `wget`, a SHA-256 tool, and systemd on Linux or
launchd on macOS. Windows x64 uses PowerShell 5.1 or newer and the current-user
Task Scheduler or Startup folder.

## Obtain the one-time claim

1. Obtain the gateway origin from the deployment operator, then fetch
   `https://<gateway>/setup/api/bootstrap-info`. Require
   `enrollment.configured=true`, then read `registrationPolicies`,
   `selfServicePolicyIds`, and `claimEndpoint`.
2. The defaults are policy `personal-default` and profile `production` (Ma/Mb/Mc).
3. POST the following JSON to the origin-relative `claimEndpoint`:

```json
{
  "deviceName": "<approved-device-name>",
  "registrationPolicyId": "<approved-policy-id>",
  "profile": "production"
}
```

If the policy is not self-service, submit HTTP Basic with fixed username
`agentweb` and the deployment's unified verify. New deployments default to
`agentwebadmin`; an upgraded gateway may advertise the retained legacy `crc`
value. The same verify is used for RGW HTTP and AWMCP tool calls. Supply a
custom deployment value through `--basic`; never put it in JSON, a URL, or logs.

Require HTTP 201 and `ok=true`. The response contains `claimUrl`, expiration,
and `oneLine.posix`/`oneLine.powershell`. The claim URL is short-lived and can
be consumed once. Do not probe it before installation, because a GET consumes
the manifest.

## Install the claim

The default POSIX install requests and consumes the claim itself:

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/refs/heads/main/install.sh \
  | sh -s -- --gateway https://gateway.example.com
```

Optional arguments include `--gateway`, `--device`, `--policy`, `--profile`,
`--basic`, `--channel`, and `--home`. Explicit `--enroll` consumes a previously
issued claim. `--device NAME --remote-gws URLS` remains the break-glass path.

On Windows x64, use the public PowerShell installer:

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/yxsicd/awrelease/refs/heads/main/install.ps1'))) `
  -Gateway 'https://gateway.example.com'
```

Its optional parameters are `-Gateway`, `-Enroll`, `-DeviceName`, `-Policy`,
`-Profile`, `-Basic`, and `-AgentWebHome`.

The installer consumes the claim, receives the signed parent enrollment token,
and exchanges it for role-bound node tokens. It handles those tokens internally;
the Agent must not parse, print, copy, or persist the parent token itself.

After installation, require local `http://127.0.0.1:17888/build-info`, the
expected clean release revision, and the intended device and manager identities.
Return to the [root Skill](../SKILL.md).
