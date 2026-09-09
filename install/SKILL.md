---
name: agentweb-install
description: Install an enrolled AgentWeb manager profile from verified public release assets.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# Install AgentWeb

Prerequisites are `curl` or `wget`, a SHA-256 tool, and systemd on Linux or
launchd on macOS.

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
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --gateway https://gateway.example.com
```

Optional arguments include `--gateway`, `--device`, `--policy`, `--profile`,
`--basic`, `--channel`, and `--home`. Explicit `--enroll` consumes a previously
issued claim. `--device NAME --remote-gws URLS` remains the break-glass path.

The installer consumes the claim, receives the signed parent enrollment token,
and exchanges it for role-bound node tokens. It handles those tokens internally;
the Agent must not parse, print, copy, or persist the parent token itself.

After installation, require local `http://127.0.0.1:17888/build-info`, the
expected clean release revision, and the intended device and manager identities.
Return to the [root Skill](../SKILL.md).
