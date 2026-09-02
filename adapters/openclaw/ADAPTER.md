---
spec_version: "1.0"
host: "openclaw"
official_docs: "https://docs.openclaw.ai/tools/skills"
last_verified: "2026-07-10"
minimum_version: "unverified"
skill_discovery: "unverified"
mcp_support: "conditional"
subagent_support: "conditional"
explicit_subagent_consent: true
registration_modes: ["project-native", "symlink", "junction", "shim"]
---
# OpenClaw adapter

## Detection

Detect the OpenClaw executable and documented Skill roots without mutating local
configuration. Record the exact version output, project root, resolved Skill
root, operating system, architecture, and whether MCP-style tools or delegation
features are enabled.

## Official docs and system support

Use the OpenClaw Skills documentation as the source of truth for Skill loading:
https://docs.openclaw.ai/tools/skills. Confirm any CLI, OS, architecture,
environment, allowlist, or dependency-gating requirements before changing an
evidence state. Do not treat community registries or another host's behavior as
OpenClaw support evidence.

## Registration

Register the hub and workflows in the documented OpenClaw Skill location.
Prefer native discovery or a link to the canonical Hub tree; use a fingerprinted
shim only when native discovery and links are unavailable or unsafe.

## MCP and subagents

Keep MCP and tool configuration in OpenClaw-owned configuration. Use
OpenClaw-native delegation only after the user explicitly authorizes multi-agent
execution.

## Evidence

This adapter remains unverified until a sanitized smoke record proves root Skill
loading, workflow Skill loading, MCP mapping when claimed, and authorized
delegation behavior on the detected OpenClaw version. Evidence must include the
system, architecture, Skill root, registration mode, and verification date.
