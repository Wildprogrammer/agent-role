---
spec_version: "1.0"
host: "opencode"
official_docs: "https://opencode.ai/docs/skills/"
last_verified: "2026-07-10"
minimum_version: "unverified"
skill_discovery: "unverified"
mcp_support: "conditional"
subagent_support: "conditional"
explicit_subagent_consent: true
registration_modes: ["project-native", "symlink", "junction", "shim"]
---
# OpenCode adapter

## Detection

Detect the OpenCode executable or app surface and documented project, user, or
custom config Skill roots without mutating configuration. Record the exact
version output, resolved Skill root, project root, operating system,
architecture, and whether MCP tools or agent/subagent features are enabled.

## Official docs and system support

Use the OpenCode Agent Skills documentation as the source of truth for Skill
loading: https://opencode.ai/docs/skills/. Confirm the current system, config
directory precedence, Claude Code compatibility behavior, and runtime feature
availability before promoting evidence. Do not infer OpenCode support from a
third-party OpenCode skill package.

## Registration

Register the hub and workflows through OpenCode's documented Skill discovery
locations. Prefer native discovery or a link to the canonical Hub tree; use a
fingerprinted shim only when native discovery and links are unavailable or
unsafe.

## MCP and subagents

Keep MCP configuration in OpenCode-owned configuration. Use OpenCode-native
agents or subagents only after explicit user consent for multi-agent execution.

## Evidence

This adapter remains unverified until a sanitized smoke record proves root Skill
loading, workflow Skill loading, MCP mapping when claimed, and authorized agent
or subagent behavior on the detected OpenCode version. Evidence must include
the system, architecture, Skill root, registration mode, and verification date.
