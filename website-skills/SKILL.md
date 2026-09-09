---
name: agentweb-website-skills
description: Discover AgentWeb capabilities through ordinary HTTPS documents.
metadata:
  service-discovery-version: "1"
  service-manifest: "../service.json"
---

# AgentWeb Website Skills

Read the root `SKILL.md` first, parse its frontmatter, and resolve
`service-manifest` against the containing document URL. Resolve every linked
Skill against its containing document URL as well. Never send credentials to a
different origin reached through an unverified link.

Website Skills provide guidance and discovery. They do not install themselves,
grant authorization, change runtime policy, or prove service readiness. Query
the selected HTTP or MCP status surface before acting.

Return to the [root Skill](../SKILL.md).
