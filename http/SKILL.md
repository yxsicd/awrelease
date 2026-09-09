---
name: agentweb-http
description: Discover and use the deployed AgentWeb HTTP interface.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# AgentWeb HTTP

On the deployed gateway, start with `GET /.well-known/agentweb`, then follow
`/api/v1` and `/api/v1/openapi.json`. Public discovery and health do not grant
control authority. Commands and peer file transfer require the deployment's RGW
header or Bearer credential.

Choose a concrete peer id. Accept execution evidence only when
`routeDecision=peer_direct` and `targetPeerId` matches that peer. Inspect the
JSON `ok` value even when HTTP status is 200. Never put credentials in query
parameters or discovery URLs.

Return to the [root Skill](../SKILL.md).
