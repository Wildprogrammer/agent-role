---
spec_version: "1.0"
host: "hermes"
official_docs: "https://hermes-agent.nousresearch.com/docs/user-guide/features/skills"
last_verified: "2026-07-10"
minimum_version: "unverified"
skill_discovery: "unverified"
mcp_support: "conditional"
subagent_support: "conditional"
explicit_subagent_consent: true
registration_modes: ["project-native", "symlink", "junction", "shim"]
---
# Hermes adapter

## Detection

Detect the Hermes executable and documented Skill root without mutating Hermes
configuration. Record the exact version output, resolved Skill root, project
root, operating system, architecture, and whether tool, MCP-style, or delegation
features are available.

## Official docs and system support

Use the Hermes Skills System documentation as the source of truth for Skill
loading: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills.
Confirm the current system, install profile, bundled Skill behavior, and update
behavior before promoting evidence. Do not infer Hermes support from the generic
Agent Skills standard alone.

## Registration

Register the hub and workflows in the documented Hermes Skill location. Prefer
native discovery or a link to the canonical Hub tree; use a fingerprinted shim
only when native discovery and links are unavailable or unsafe.

## MCP and subagents

Keep tool and MCP-style integration data in Hermes-owned configuration. Use
Hermes-native delegation only after explicit user consent for multi-agent
execution.

## Evidence

This adapter remains unverified until a sanitized smoke record proves root Skill
loading, workflow Skill loading, tool or MCP mapping when claimed, and
authorized delegation behavior on the detected Hermes version. Evidence must
include the system, architecture, Skill root, registration mode, and
verification date.
