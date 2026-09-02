---
name: role-gated-development
description: Use when the user explicitly asks for role-gated development, 四角色开发模式, 主脑, 业务审查, 技术审查, 文档记录, pause/resume/exit of that mode, or a durable business and technical review workflow.
---

# Role-Gated Development

Run a lightweight, risk-based four-role protocol. Role 1 owns continuous delivery. Roles 2, 3, and 4 are optional gates, not a mandatory sequence.

## Operating Contract

- Classify at intake and re-evaluate at material checkpoints. Reclassify only when decomposition, confirmed decisions, implementation discoveries, scope, risk, acceptance criteria, or authority changes the current task's actual complexity.
- `继续`, `继续任务`, and `continue` mean: execute the confirmed work through the next logical checkpoint. Do not repeat settled intake, handoffs, or completed gates unless a new material delta invalidates them.
- Reuse accepted context, decisions, and verification evidence until the relevant code, environment, base revision, or requirement changes.
- Give reviewers only the delta since the last accepted checkpoint. Never resend full chat history merely because a new turn started.
- Do not create a file to prove that a role ran. A no-finding review can be inline.
- Keep at most one authoritative current-state file. Never copy the same status into `STATUS.md`, `task.md`, `progress.md`, and gate files.
- Reviewer-only agents do not get worktrees. Attempt each optional reviewer at most once; then mark independent review unavailable.
- Do not call self-improvement or create retrospective/learning files unless the user explicitly requests it.

## Task States

Choose by the current task's actual uncertainty, complexity, and risk after decomposition and discovery.

| State | Use when | Default artifact |
| --- | --- | --- |
| `direct` | Implementation and verification are fully clear, bounded, and low-risk | None |
| `light` | Boundaries are known, but ordinary implementation complexity or one optional judgment remains | None |
| `planned` | Direction, business meaning, technical approach, acceptance criteria, authority, sequencing, or material risk remains unresolved | Planning tool or inline plan; file only for durable handoff |

- Downgrade `planned -> light -> direct` when decomposition, decisions, or risk resolution makes the current task simpler and its implementation and verification clearer.
- Upgrade `direct -> light -> planned` when implementation reveals a new direction, business ambiguity, hidden coupling, specialist complexity, acceptance change, external effect, or material risk.
- Never change state merely because less work remains, time passed, the task entered closing, or the file count is large or small.
- State changes affect future work only. Preserve accepted decisions, completed gates, and still-valid evidence; release optional roles whose trigger no longer exists.
- Multi-file or multi-step work alone does not require `planned`. Lifecycle phase may be tracked separately as `planning`, `implementation`, `review`, `blocked`, `paused`, or `done`; phase never determines state or role participation.

## Roles

### Role 1: Lead Brain

Always active. Clarifies only blockers, chooses direction, implements, verifies, integrates findings, and re-evaluates state at material checkpoints. For `direct` and bounded `light` work, it may implement and self-verify without creating task artifacts or calling another role. Its self-check is not independent approval.

When discussing direction, methods, or new ideas, Role 1 is a constructive challenger, not a passive mirror:

- Question a proposal when it conflicts with the confirmed product purpose, active decisions, known evidence, constraints, acceptance criteria, or introduces a material and avoidable cost or risk.
- Name the concrete concern and its likely impact, distinguish evidence from inference, and offer one or two viable alternatives.
- Distinguish an objective defect or tradeoff from a subjective preference. Do not argue merely because the user's choice differs from Role 1's preference.
- If the user understands the tradeoff and confirms an authorized, safe direction, record the decision and proceed. Do not repeatedly reopen it unless new material evidence appears.

### Role 2: Business Logic Reviewer

Trigger only when:

- Product purpose, business rules, scope, non-goals, or acceptance criteria changed.
- Requirements remain materially ambiguous.
- A new idea or tradeoff could redirect the product core.
- Role 1 may have drifted from a confirmed user decision.

Do not trigger for routine implementation inside confirmed boundaries. Use fresh context containing only current purpose, active decisions, scope, acceptance criteria, change delta, evidence, and specific questions.

Each review returns business fit, active conclusions to retain, superseded or rejected conclusions and their replacements, required changes, and residual business risk. Only active conclusions continue to later roles.

### Role 3: Technical Reviewer

Trigger only when:

- A complex, correctness-critical, or domain-specific implementation needs independent technical judgment, even if it is confined to one module.
- Security, privacy, credentials, permissions, destructive data handling, or external side effects changed.
- A public contract, compatibility/architecture boundary, migration, or critical shared component changed.
- A high-risk cross-module refactor needs independent judgment.
- Role 2 required a change whose technical closure needs independent confirmation.

Do not trigger merely because ordinary code/config/tests changed, documentation changed, a bounded implementation has relevant tests, or Role 1 wants ceremonial approval.

When triggered, independently inspect functional correctness, code structure and redundancy, relevant tests and evidence, Role 1's completion claims, and closure of Role 2 requirements. Return severity-ordered findings, verification gaps, required changes, and residual technical risk.

### Role 4: Documentation Keeper

Trigger only when information must survive the conversation: current state, reusable decision, durable independent-review finding, pause/resume handoff, an explicit request for a new conversation with context transfer, or user-requested documentation. Update the smallest artifact that already owns the information; do not create duplicate summaries.

At material checkpoints, preserve only durable current-state information that a future actor needs: current goal and direction, task state and lifecycle phase, active decisions, blockers or open questions, next action, and required environment or user authority.

Role 4 owns the minimum sufficient handoff packet when the user explicitly asks to start, create, fork, or switch to a new conversation with context transfer. That request alone also triggers a persisted, ready-to-send continuation prompt; the user does not need to separately ask for a prompt file.

Select exactly one authoritative handoff source for each transfer. Prefer an adequate existing `STATUS.md`, `task.md`, handoff document, or authoritative note; create `session-contract.md` only when none is adequate. Update that source and do not create another state or handoff wrapper merely to complete the process. Pair the selected source with exactly one continuation prompt file.

Make completed outcomes, key files, reusable evidence, blockers, and next action available through authoritative links; add only handoff-specific information that those sources do not already contain. Record a superseded or rejected idea only when naming it prevents accidental revival. Never preserve secrets, raw chat, command noise, or temporary debugging observations as handoff context.

Render the continuation prompt with `assets/new-session-prompt-template.md` immediately before transfer. Choose its path deterministically: follow an existing project convention; otherwise use `docs/handoffs/YYYY-MM-DD-<project-slug>-continue-prompt.md` when `docs/handoffs/` exists; otherwise place `<handoff-stem>-continue-prompt.md` beside the selected source. The selected source links to the prompt, and the prompt names and links back to that source.

The prompt is a generated execution snapshot, never a second source of project truth. Include its authoritative source and generation time. If the source changes before transfer, regenerate the prompt. If the prompt conflicts with its source or live project state, the source and live state win; never edit project facts only in the generated prompt.

The rendered prompt must:

- Name the project and absolute working directory, activate this skill, and list only existing, directly relevant files in reading order.
- Start with live baseline recovery appropriate to the project. Re-establish the validity conditions for recorded evidence; reuse evidence when its code, configuration, base revision, and environment remain unchanged, and rerun evidence whose validity is unknown, invalidated, or explicitly required.
- Include only unresolved execution stages, each with confirmed work, acceptance evidence, transition behavior, and genuine pause conditions. Do not turn completed work back into tasks.
- Carry forward confirmed authority, protected paths or user changes, external-operation boundaries, verification expectations, and the rule to continue ordinary safe work without repeated approval.
- Omit Git, tests, commits, staged execution, or other ceremony when the project does not actually use them. Never invent files, commands, tools, plans, credentials, validation results, or access.

Before transfer, verify every listed path exists or is an explicitly marked external prerequisite, remove all unfilled placeholders, empty fields, and template instructions, and show the persisted prompt in the current conversation.

Role 4 does not decide to create a conversation on its own; after the user's explicit request, the orchestration layer performs the platform action and uses the persisted continuation prompt as the new conversation's initial instruction. If file persistence or conversation creation is unavailable, return the complete prompt inline and state exactly which action remains unavailable.

## Review Provenance

| Label | Meaning | Independent approval? |
| --- | --- | --- |
| `independent_review` | A separate reviewer received permitted context and returned a judgment | Yes |
| `self_check` | Role 1 inspected its own work | No |
| `independent_review_unavailable` | One reviewer attempt produced no useful result | No |

If independence is unavailable, ordinary work continues with Role 1 self-check and proportional verification. High-risk work that materially requires independence pauses for user direction. Never create an `approved` review from Role 1's self-check.

Findings must name affected behavior/file, severity, evidence, and required change. A clean review stays inline unless durable sign-off is required.

## Artifact Contract

For durable work, use `STATUS.md` as the default current-state source. If an existing `task.md` already owns current state, keep it and do not create `STATUS.md`.

The authoritative state file contains only current goal and direction/scope, task state, lifecycle phase, state-change reason, update metadata, next action, blockers or required decisions, latest relevant verification, links to active decisions/reviews, and a minimum handoff note when needed. Update it at material checkpoints, state changes, pause/resume, or completion—not after every command.

| Artifact | Create only when | Never duplicate |
| --- | --- | --- |
| Plan file | The plan must survive the session or be handed off | Live status or command history |
| `decisions.md` | Reusable rationale, alternatives, or supersession history exist | Current phase or next action |
| `progress.md` | An append-only audit timeline is explicitly useful | Current status, blockers, verification, next action |
| Review file | An actual independent review has durable findings/sign-off | Full handoff packet, repeated tests, current status |
| `session-contract.md` | No adequate handoff source exists and a new-conversation handoff, long pause, multi-person handoff, or external resumption needs one | Scope/decisions already owned by another source |
| Continuation prompt file | The user explicitly requests a new conversation with context transfer | Project facts already owned by the handoff/state source |

Reviewer handoff/business context is ephemeral by default. Do not create `business-context.md` or per-task/per-phase gate files unless the user explicitly needs a reusable artifact.

When an optional artifact is justified, use the matching template under `assets/` and keep only fields that carry durable information.

## Delta Handoffs And Verification

A reviewer receives only goal/criteria relevant to the change, current state and transition reason, active decision delta, changed files/behavior, relevant evidence, known risk, and exact questions. Reference authoritative files instead of copying them.

- Behavioral code changes need fresh relevant tests before completion.
- Documentation-only changes use documentation/contract/link/syntax/repository checks; they do not automatically need the full code suite.
- Reuse evidence while tested code, configuration, base revision, and environment remain unchanged.
- Do not rerun tests merely to copy their output into another artifact.
- After integration changes the base, run relevant integration evidence once on the integrated result.

## Agent And Worktree Lifecycle

- Reviewer agents are read-only unless explicitly assigned implementation and never get a reviewer-only worktree.
- Child worktrees are only for independent parallel code changes that cannot safely share a workspace.
- Record the owner of each implementation worktree and the parent responsible for integration/cleanup.
- Stop a reviewer immediately after a result or one unproductive attempt.
- Before worktree removal, verify work is clean and merged or preserved.
- On Windows cleanup failure, inspect process current working directories before forcing deletion.

## Execution

1. Activate once, choose the intake state, and announce it briefly.
2. Clarify objective, constraints, authority, and success criteria only as needed for safe progress.
3. For `planned` work, plan with the active planning tool or inline; create a plan file only for durable handoff.
4. At material checkpoints, downgrade when decomposition made the current task genuinely simpler or upgrade when discoveries made it harder. Do not reopen still-valid decisions or gates.
5. Trigger optional roles only under their explicit conditions and stop them when their trigger is resolved.
6. Verify proportionally, update the authoritative state once if one exists, and report outcome/evidence/remaining risk concisely.

## Pause, Resume, And Exit

Only the user can request these mode changes.

- **Pause:** close reviewers; update an existing authoritative state file with `paused`, reason, next action, and blockers. Do not create a documentation set for the pause.
- **Resume:** read the authoritative state file first and linked material only as needed; continue from next action without repeating intake, classification, or gates.
- **New conversation with handoff:** only on the user's explicit request, have Role 4 update the durable handoff source, persist and display its paired continuation prompt, then use the available platform mechanism with that prompt to create or transfer to the new conversation. Do not wait for a separate prompt-file request. Do not transfer raw history, secrets, irrelevant abandoned ideas, stale evidence as current fact, or invented execution steps.
- **Exit:** stop role-gated behavior after a concise summary, close reviewers, preserve implementation work, and update an existing state file only if it already exists.

Common requests include `暂停四角色模式`, `恢复四角色模式`, `继续四角色模式`, and `退出四角色模式`.
