---
name: agentweb-mcp
description: Connect to the existing AgentWeb AWMCP Streamable HTTP service.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# AgentWeb MCP

Use the AWMCP `/mcp` URL advertised by the deployed gateway descriptor or
provided by the operator. Do not append `/mcp` to an AgentGW hostname unless
that deployment explicitly publishes such a route.

Initialize Streamable HTTP and call `tools/list`. The stable Agent Kernel has
eight tools: `service_metadata`, `topo`, `operation_get`, `skill_list`,
`skill_get`, `skill_run_read`, `skill_run_write`, and `skill_run_publish`.
Discover one Skill and action before calling a runner. Every `tools/call`
requires the operator-provided `verify` value; discovery does not.

The release integration does not change AWMCP transport negotiation, tool
names, arguments, authentication, verification, or response semantics. Return
to the [root Skill](../SKILL.md).
