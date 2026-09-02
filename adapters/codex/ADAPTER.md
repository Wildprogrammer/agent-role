---
spec_version: "1.0"
host: "codex"
official_docs: "https://developers.openai.com/codex/skills"
last_verified: "2026-07-10"
minimum_version: "unverified"
skill_discovery: "unverified"
mcp_support: "conditional"
subagent_support: "conditional"
explicit_subagent_consent: true
registration_modes: ["project-native", "symlink", "junction", "shim"]
---
# Codex adapter

## Detection

Detect the Codex surface before changing anything: ChatGPT desktop Codex,
Codex CLI, or IDE extension. Record the executable or app version, resolved
Skill root, project root, operating system, architecture, and whether the
session exposes MCP or subagent tools.

## Official docs and system support

Use the OpenAI Developers Agent Skills page as the source of truth for Codex
Skill behavior: https://developers.openai.com/codex/skills. Before promoting
support, verify the current Codex surface, operating system, architecture, and
runtime behavior against that documentation and a local smoke test. Do not infer
support from another host.

## Registration

Register `agent-workflow-hub` and each workflow under a directory matching the
Skill `name`. Prefer Codex-native Skill discovery when documented for the
current surface. If native discovery is unavailable, prefer a symlink or Windows
junction that points at the canonical Hub Skill; use a fingerprinted shim only
when links are unavailable or unsafe.

## MCP and subagents

Keep MCP configuration in Codex-owned configuration and never copy another
host's MCP shape. Use subagents only after the user explicitly authorizes
multi-agent execution for the current task.

## Evidence

This adapter remains unverified until a sanitized smoke record proves root Skill
loading, workflow Skill loading, MCP mapping when claimed, and authorized
subagent behavior on the detected Codex version. Evidence must include the
system, architecture, Skill root, registration mode, and verification date.
