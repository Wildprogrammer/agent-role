---
name: agent-workflow-hub
description: Use when an Agent needs to discover, prepare, diagnose, run, update, or clean portable workflows in this repository.
compatibility: Registered through Codex, OpenClaw, Claude Code, Hermes, or OpenCode adapters.
metadata:
  spec-version: "1.0"
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# Agent Workflow Hub

## Repository root

Define `HUB_ROOT` once from the canonical source `SKILL.md` location exposed by native registration, or from the source-path exposed by an adapter or shim; resolve and canonicalize it. Verify that `SKILL.md`, `pyproject.toml`, and `src/agent_workflow_hub/` exist under `HUB_ROOT`; stop if any marker is missing.

Resolve every relative workspace, workflow, output, and authoring path documented here against `HUB_ROOT`. Never use a caller's unrelated working directory. A workflow-local configuration parser may document an explicit, narrower path base for values inside that configuration file; this is an exception only for those values and must reject path escape. Run `workflow-hub validate <absolute HUB_ROOT>`; never run validation against `.`.

Only CLI marker mode establishes a Hub repository. Direct library validation without markers is structural fixture mode and cannot establish a runnable Hub.

## Operating rules

1. Read the selected `HUB_ROOT/workflows/<name>/SKILL.md` completely. The selected workflow is authoritative.
2. Unsupported features are not inferred; never infer speaker diarization.
3. Run read-only diagnostics first.
4. The selected capability's required `installation` contract is the only authorization source for installation. Run detection first and never infer a more permissive policy from a command, host, or workflow.
5. Run 渐进式只读发现 before declaring a capability missing: inspect PATH、注册表、包管理器清单、application aliases、shortcuts、capability-declared locations and Hub workspace locations first; then inspect common installation directories and bounded candidate-name searches. Discovery reads only paths, file metadata and versions. It never reads unrelated user-document contents, credentials directories, `.codex-remote-attachments/`, or user workflow outputs, and reports its searched scope, elapsed time and hits.
6. The selected capability's `installation` contract remains the only installation authorization source. Apply the declared methods, source, locked version, integrity, destination, network use and expected disk impact at one of three levels: 低风险、中风险、高风险. Low risk is read-only inventory and discovery. Medium risk is a non-elevated, reproducible install of a declared ordinary package or CLI into its declared runtime or workspace after reporting the exact command and rollback. High risk includes browser kernels, large desktop applications, drivers, model weights, elevation, services and host configuration; it needs a separate explicit confirmation.
7. For `agent-managed` capabilities, the Agent may use only declared methods at the confirmed risk level. A `global-runtime` scope explicitly permits declared `pip`, `uv`, or `npm` writes to the selected reusable runtime; it does not permit an undeclared system package manager, driver, installer, host configuration, privilege escalation, or a Git clone. A declared `git` method means a verified Git clone only: clone the canonical HTTPS `official_source` into the declared `workspace-shared` or `workspace-workflow` destination with `git clone --no-checkout <source> <destination>`, then use `git -C <destination> checkout --detach <locked commit>` and compare `git -C <destination> rev-parse HEAD` with the capability's `git-commit` integrity value. Do not clone branch or tag aliases such as `main`, `master`, or `latest`; never clone into `global-runtime`.
8. For `user-managed` capabilities, `user-managed` means user-owned maintenance by default, not a blanket prohibition. The Agent may run a capability-declared method only when the declared method, source, locked version, integrity, destination and confirmed risk level authorize it; otherwise it writes localized guidance at an approved output path in the language the user is currently using. Never infer an undeclared package manager, source, version, destination, MCP registration or system/host configuration change. Guidance includes the official source, immutable version, integrity, system and architecture, disk, permissions, network, rollback, host evidence, and read-only post-install checks.
9. Never skip confirmation gates.
10. Keep `workspace-shared` dependencies only in `workspace/shared/`, `workspace-workflow` dependencies only in `workspace/workflows/<name>/`, and user deliverables in `workflows/<name>/outputs/`. New reusable runtimes default to `<HUB_ROOT>/workspace/shared/runtimes/<runtime-id>/<version>/`; `global-runtime` retains its cross-workflow reuse meaning but not an external default location. Existing legacy runtimes may be detected read-only and used, but are never moved or deleted automatically. Resolve all project paths against `HUB_ROOT`.
11. Adapters and agents must call `load_role_snapshot` immediately before role use and consume the returned snapshot content and digest. Never validate and then re-open a role by path; stop on any identity or digest change.

### 工作流能力归属

单一领域工作流拥有可独立复用的领域能力、输入协议、执行实现、结果契约和回读方式；复合工作流拥有跨工作流编排、阶段状态、业务 Gate、跨域数据绑定和失效关系。仅服务于复合场景的约束不得下沉为单一工作流的通用限制，复合工作流只选用能力子集时也不得据此缩减领域工作流已有或合理应有的能力。

复合工作流不得复制领域协议、参数校验、渲染、传输或回读实现；领域工作流不得反向依赖复合工作流的状态机、Gate 或专属分支规则。只有对该领域独立使用场景普遍成立的规则才能归入单一工作流。能力声明、实现和调用不一致时，优先修复能力所有者，不得在调用方增加替代实现或把缺口固化为限制。

All durable plans, installation guides, decisions, progress records, session handoffs,
and other generated process documents must use the language currently used by the
user. Keep commands, paths, identifiers, and version values exact; do not silently
fall back to English or the operating-system locale.

## Workflow catalogue

Dynamic enumeration is authoritative: after `workflow-hub validate <absolute HUB_ROOT>` succeeds, `workflow-hub list <absolute HUB_ROOT>` is the authoritative dynamic source for workflow enumeration. Table rows never override validation or its validated catalogue.

| State | Meaning |
|---|---|
| `catalogued candidate` | Named by Hub only. |
| `structurally valid` | A canonical workflow exists and full repository contract validation passes. |
| `executable on host` | For a specific host when actual capability detection passes for the selected capabilities and all fixed policy and confirmation gates pass. Host compatibility is diagnostic and must not block execution solely because adapter evidence is unverified. |
| `locally ready` | The read-only doctor passes on the current system, tool versions, paths, and permissions. |

Structurally valid never means executable on a host or locally ready. Host compatibility reports `verified`, `conditional`, `unverified`, or `unsupported` evidence independently from actual capability detection and local readiness.

The rows below are catalogued workflows. Dynamic validation determines their current structural state; host execution and local readiness still require the evidence described above.

| Workflow | Purpose |
|---|---|
| `3d-printing` | Catalogued workflow. Confirm manufacturing constraints, model in Blender, validate the mesh, slice with an approved profile, and deliver reviewed G-code without starting a printer. |
| `meeting-notes` | Catalogued workflow. Import or record approved audio, transcribe locally, obtain human review, optionally summarize, and write approved Markdown to Obsidian. |
| `daily-assistant` | Catalogued workflow. Normalize user-supplied daily tasks and explicit progress, propose priority/classification, and produce requested local daily reports or task-tracker drafts without external writes. |
| `bead-pattern` | Catalogued workflow. Convert a user-provided image to a reviewed fixed-palette bead-pattern candidate and deliver one coded PNG chart. |
| `image-ocr` | Catalogued workflow. Extract reading-order plain text from explicitly provided local image files through a selected local OCR engine. |
| `information-collection` | Catalogued workflow. Collect, filter, and summarize explicitly scoped web sources, optionally render a requested file, and deliver through an approved host channel. |
| `jenkins-operations` | Catalogued workflow. Inspect and operate a user-managed Jenkins Controller through its locally configured typed MCP; Jenkins account/RBAC is the default authorization boundary, explicit policy/confirm_writes settings are optional, and production HTTP writes stay behind a hard gate. Never install Jenkins or register a host MCP mapping automatically. |
| `knowledge-support-agent` | Catalogued workflow. Build and incrementally refresh one local evidence-backed knowledge Agent from configured committed Git snapshots, documents, collected web material, and user-confirmed experience, with hybrid retrieval and automatic full-text fallback. |
| `requirements-analysis` | Catalogued workflow. Analyze authorized requirement sources and historical evidence, clarify ambiguity, generate fixed-column use cases, and return a reviewable Gate 1 candidate without changing a repository. |
| `git-operations` | Catalogued workflow and the Hub's single Git workflow. Run general Git status, diff, log, shallow clone, add, commit, branch, checkout, merge, push, exact non-force push, and remote ref queries through a minimal argv-only CLI with strict SHA, branch, ref, remote, and repository-path validation; protect uncommitted user content; Git's own credentials and permissions are the default boundary. |
| `test-reporting` | Catalogued workflow and the Hub's single Python authority for the report model, classification, Markdown renderer, and UTF-8 byte hash (`agent_workflow_hub.test_reporting`). Organize user-provided existing test materials into a canonical Markdown test report; Jenkins/JUnit are an optional input path, not a requirement. |
| `mysql-operations` | Catalogued workflow. Independently inspect one externally configured MySQL target through fixed metadata/read, structured DML, and guarded migration tools. |
| `ssh-operations` | Catalogued workflow. Connect to configured Windows, macOS, or Linux SSH targets to run commands and related steps, manage files through SFTP/SCP, traverse jump hosts, and open explicit port forwarding with TOFU host-key protection and one confirmation only for high-impact operations. |

## Prepare / bootstrap

Select only a structurally valid workflow and read it and its capability contracts completely from `HUB_ROOT`. Run read-only detection before proposing changes. Use actual capability detection for the selected capabilities and require doctor readiness before execution. Host compatibility is diagnostic; it must not block execution solely because adapter evidence is unverified. Fixed policy and confirmation gates remain mandatory. When setup is missing, follow each selected capability's `installation` contract: perform only bounded `agent-managed` setup; otherwise create a localized `user-managed` guide at a user-approved output path. A failed or incomplete safety condition downgrades the operation to guidance rather than trying another installer or package manager.

## Doctor

Stay strictly read-only. Check the host, OS, architecture, hardware, versions, paths, permissions, official links, locks, and available evidence. Report evidence states and copyable remediation steps; `unverified` does not mean supported.

## Update

Update only the capability, adapter, authoring source, or workflow-local role explicitly named by the user. First show compatibility, license, and behavior changes. Never rewrite workflow-local roles automatically as a side effect of an `agency-agents` source update; user-requested workflow-local role maintenance is allowed. Rerun the affected contract and smoke tests afterward.

## Clean

First produce a dry-run with canonical absolute paths and sizes. The workspace is not automatically disposable. Default cleaning may include only targets explicitly classified by their capability or workflow manifest as rebuildable cache, temporary, or checkpoint state. Preserve unclassified files, credentials, `.env.local`, printer profiles, model profiles, original media, and other non-rebuildable private configuration unless the user separately names an external file operation, which Hub clean must not perform. For `outputs/`, require a named workflow, an exact path list, and second explicit confirmation (二次确认).

Reject empty paths, repository or filesystem roots, home directories, traversal, symlinks, junctions, external Vaults, and original media. Immediately before actual deletion, revalidate every target and reject anything outside the approved scope.

## Authoring roles

`agency-agents` is not preinstalled. While authoring or materially extending a workflow, first decide whether a specialized role significantly improves domain judgment, safety/compliance, or delivery quality; cosmetic wording improvements do not justify this prompt. If it may improve a workflow and the local source cache is absent, ask the user whether to make a controlled local clone of the declared official source into `workspace/shared/authoring/agency-agents/` at its fixed commit. Before asking, state the official HTTPS source, fixed commit, destination, network use, and expected disk impact. Never use a remote agency-agents source and never make the source catalog a runtime dependency.

The user-approved authoring source is a narrow exception to capability installation. It must use the declared official HTTPS URL and fixed commit only: run `git clone --no-checkout <source> <destination>`, `git -C <destination> checkout --detach <locked commit>`, then require `git -C <destination> rev-parse HEAD` to equal the declared commit. Never use a branch or tag, and do not automatically pull. Before every reuse, verify that the cache resolves inside `HUB_ROOT/workspace/shared/authoring/agency-agents/`, is not a symlink or junction, origin matches the declared official HTTPS source, HEAD is detached at the declared commit, and the working tree is clean. If any check fails, do not use or overwrite it automatically; report the mismatch and ask the user how to proceed.

If the user declines, the source is unavailable, or no supplied role fits, construct a workflow-local role when it adds practical value. Omit a role when it adds no value; neither outcome blocks the workflow. A selected or constructed role must be a UTF-8 workflow-local `roles/<name>.md` file and must be declared in `metadata.roles`; immediately before use, load its snapshot with `load_role_snapshot` and use the returned content and digest.

Each role adapted from the source records its source URL, fixed commit/version, license, copied concepts, and local modifications. A self-authored role records `source: local`, its purpose, and local modifications; it does not invent external provenance. Existing workflow-local roles never auto-update when the shared authoring source changes.
