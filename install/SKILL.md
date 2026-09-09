---
name: agentweb-install
description: Install an enrolled AgentWeb manager profile from verified public release assets.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# Install AgentWeb

Prerequisites are `curl` or `wget`, a SHA-256 tool, systemd on Linux or launchd
on macOS, and a single-use enrollment claim issued for the intended device.

```sh
curl -fsSL https://raw.githubusercontent.com/yxsicd/awrelease/main/install.sh \
  | sh -s -- --enroll '<claim-url>'
```

Optional arguments are `--channel dev|main|prod` and `--home PATH`. Normal
installations use `prod`. Explicit `--device NAME --remote-gws URLS` is a
break-glass recovery path and requires operator-provided topology; never derive
it from public examples.

After installation, require local `http://127.0.0.1:17888/build-info`, the
expected clean release revision, and the intended device and manager identities.
Return to the [root Skill](../SKILL.md).
