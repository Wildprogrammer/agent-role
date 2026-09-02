---
spec_version: "1.0"
host: "claude-code"
official_docs: "https://code.claude.com/docs/en/skills"
last_verified: "2026-07-10"
minimum_version: "unverified"
skill_discovery: "unverified"
mcp_support: "conditional"
subagent_support: "conditional"
explicit_subagent_consent: true
registration_modes: ["project-native", "symlink", "junction", "shim"]
---
# Claude Code adapter

## Detection

Detect the Claude Code executable or app surface and documented project or user
Skill roots without changing configuration. Record the exact version output,
resolved Skill root, project root, operating system, architecture, and whether
MCP and subagent features are available in the current surface.

## Official docs and system support

Use the Claude Code Skills documentation as the source of truth for this host:
https://code.claude.com/docs/en/skills. Confirm the current system, runtime,
Skill root precedence, and feature availability before promoting evidence. Do
not infer Claude Code support from Claude web, Claude platform, or another host.

## Registration

Register the hub and workflows through Claude Code's documented Skill discovery
locations. Prefer native discovery or a link to the canonical Hub tree; use a
fingerprinted shim only when native discovery and links are unavailable or
unsafe.

## MCP and subagents

Keep MCP configuration in Claude Code-owned configuration. Invoke Claude
Code-native subagents only after explicit user consent for multi-agent
execution.

## Evidence

This adapter remains unverified until a sanitized smoke record proves root Skill
loading, workflow Skill loading, MCP mapping when claimed, and authorized
subagent behavior on the detected Claude Code version. Evidence must include
the system, architecture, Skill root, registration mode, and verification date.
