---
name: agentweb-install
description: Install an enrolled AgentWeb manager profile from verified public release assets.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# Install AgentWeb

Prerequisites are `curl` or `wget`, a SHA-256 tool, systemd on Linux or launchd
on macOS, and an operator-approved target gateway and device name.

## Obtain the one-time claim

1. Fetch `https://<approved-gateway>/setup/api/bootstrap-info`. Require
   `enrollment.configured=true`, then read `registrationPolicies`,
   `selfServicePolicyIds`, and `claimEndpoint`.
2. Use the operator-selected registration policy and `production` or `dv`
   profile. Do not select a policy merely because it appears in discovery.
3. POST the following JSON to the origin-relative `claimEndpoint`:

```json
{
  "deviceName": "<approved-device-name>",
  "registrationPolicyId": "<approved-policy-id>",
  "profile": "production"
}
```

If the selected policy is listed in `selfServicePolicyIds`, the POST needs no
credential. Otherwise it requires an operator-supplied management credential
in the `x-agentweb-rgw-token` header. The Agent cannot derive that credential
from this repository or from gateway discovery; ask the operator to authorize
the claim or to provide the generated one-line command through an approved
secret channel. Never place the credential in JSON, a URL, chat, or logs.

Require HTTP 201 and `ok=true`. The response contains `claimUrl`, expiration,
and `oneLine.posix`/`oneLine.powershell`. The claim URL is short-lived and can
be consumed once. Do not probe it before installation, because a GET consumes
the manifest.

## Install the claim

Prefer the exact platform command returned by the gateway. The POSIX form is:

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --enroll '<claim-url>'
```

Optional arguments are `--channel dev|main|prod` and `--home PATH`. Normal
installations use `prod`. Explicit `--device NAME --remote-gws URLS` is a
break-glass recovery path and requires operator-provided topology; never derive
it from public examples.

The installer consumes the claim, receives the signed parent enrollment token,
and exchanges it for role-bound node tokens. It handles those tokens internally;
the Agent must not parse, print, copy, or persist the parent token itself.

After installation, require local `http://127.0.0.1:17888/build-info`, the
expected clean release revision, and the intended device and manager identities.
Return to the [root Skill](../SKILL.md).
