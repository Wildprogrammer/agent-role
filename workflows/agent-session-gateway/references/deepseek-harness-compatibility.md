# DeepSeek Harness Web compatibility

The gateway supports exactly `0.2.0-rc.2`, `0.2.1-rc.5`, and `0.2.2-rc.1`.
The configured `expected_version` must equal the installed package version.
An unsupported version fails closed; it is never routed to Hermes or a new
headless process as a substitute for the selected Web session.

| Operation | 0.2.0-rc.2 / 0.2.1-rc.5 | 0.2.2-rc.1 |
| --- | --- | --- |
| Authentication | Published launch URL exchanged for an HTTP-only cookie | Same browser authentication remains supported |
| Agent/session discovery | `agentPresets/list`, `session/list` | Same endpoints |
| Submit / cancel / create | `session/prompt`, `session/cancel`, `session/create` | Same endpoints and request envelopes |
| Read reply events | `session/page` | `session/events`; upstream retains `page` as a deprecated alias |
| Binary attachment | `session/uploadFileBinary`, then prompt receipt ID | Same receipt flow; images additionally send `mediaType` to select upstream image processing |
| Compress | Not verified; explicitly unsupported | Not verified; explicitly unsupported |

Submission must return `accepted: true`. An ambiguous acknowledgement is an
unknown outcome, never permission to replay. Reply correlation still uses the
submitted request ID and the selected canonical session. Cancellation acceptance
is only reported as `requested` until completion is known.

## Credentials and upgrades

A launch token belongs to one running Web process. Independently restarting or
upgrading Harness invalidates an old launch token, even when the port is unchanged.
Changing `expected_version` alone does not renew it. Use a freshly published token
in the configured environment variable, or explicitly restart under the managed
launcher so it can capture the new token privately. Never derive a browser cookie
from persisted signing secrets, retain launch URLs in logs, or stop an independently
started Harness process without authorization.

Preserve the same `dsh_home`, selected Preset, canonical session IDs and gateway
bindings. Restarting can interrupt active turns and plugins; inspect the current
process before deployment. Never reuse historical PIDs as shutdown instructions.

## Validation

The ordinary gateway tests cover exact version gates, event endpoint selection,
image media type routing, receipt handling and ambiguous acknowledgement behavior.

An optional test uses the installed **0.2.2-rc.1** connection implementation and
generated RPC argument schemas with in-memory credentials and synthetic sessions:

```powershell
$env:DSH_NATIVE_TEST_ENTRY = '<installed @deepseek-ai/dsh>/lib/bin.js'
python -m pytest workflows/agent-session-gateway/tests/test_dsh_native_protocol.py -q
```

Node must be on PATH. This test loads no user profile, invokes no model and does
not authenticate to or write to a real Agent session. It validates native HTTP
authentication, envelopes and request schemas; synthetic replies are not evidence
of live Agent execution or upstream image conversion. After authorized deployment,
verify current-host authentication and read-only discovery before a user message.
