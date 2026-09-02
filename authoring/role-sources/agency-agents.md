# agency-agents role source

- Official source: https://github.com/msitarzewski/agency-agents
- Immutable source commit: `fc5a192e7e0f2fad0d74686d9165435e410869a8`
- Verification: confirmed from the official API on 2026-07-02
- License: MIT
- Use: authoring-time reference only
- Cache target (resolved against `HUB_ROOT`): `workspace/shared/authoring/agency-agents/`

Acquisition is opt-in: the project does not preinstall this source. When a specialized role significantly improves domain judgment, safety/compliance, or delivery quality and this local cache is absent, ask the user whether to make the controlled local clone at the fixed commit. State the official HTTPS source, fixed commit, destination, network use, and expected disk impact before asking. This authoring source is a narrow exception to capability installation: use only `git clone --no-checkout`, detached checkout of the fixed commit, and `rev-parse HEAD` integrity verification. Never read the remote source at workflow runtime, pull it automatically, or use a branch/tag alias.

Before reusing the cache, verify its resolved path stays inside the declared target and is not a symlink/junction, its origin is the official HTTPS source, HEAD is detached at the fixed commit, and the working tree is clean. A mismatch must not be overwritten or used automatically; report it and ask the user. If the user declines, use a locally constructed workflow role when useful, or omit a role when it adds no value.

Existing workflows never auto-update when this source changes. When adapting it, record the source URL, commit/version, license, copied concepts, and local modifications in the workflow-local role.
