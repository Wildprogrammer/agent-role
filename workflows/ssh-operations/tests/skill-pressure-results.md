# SSH Operations Skill Pressure Results

## RED baseline — no ssh-operations Skill

1. The ownership scenario rejected use of environment-validation, but added host, fingerprint, diff, certificate, service, maintenance-window, backup, rollback, overwrite, and two final confirmations before ordinary authorized test work.
2. The multi-platform scenario replaced confirmed TOFU with strict pre-provisioned fingerprints, added first-host confirmation, imposed key-preference and local file-permission policy, and broadened confirmations beyond the agreed high-impact boundary.
3. The implementation-pressure scenario rejected shell=True, but proposed copying environment-validation read-only constraints and automated-test-lifecycle Gates, forbidding general commands, requiring signed non-reusable receipts, and staging release by risk.

## GREEN expectation

With ssh-operations loaded, select the domain workflow, use AsyncSSH, preserve TOFU, allow ordinary authorized writes without extra gates, require one confirmation only for the documented high-impact set, and never import lifecycle/read-only constraints.

## GREEN contract evidence

1. The Skill assigns command execution, related steps, authentication, TOFU, SFTP/SCP, jump hosts and forwarding to `ssh-operations`; it explicitly rejects importing environment-validation's read-only boundary.
2. First-use TOFU records without a prompt, later mismatch rejects without overwrite, and Agent Forwarding remains off unless the target explicitly enables it.
3. Ordinary commands, sudo, uploads and explicit overwrite continue without a generic write gate; only structured delete or a command explicitly classified high impact by the main Agent requires one confirmation.

These observations are enforced by local contract and behavior tests. Independent DeepSeek pressure replay was unavailable because the configured ChatGPT account did not expose the requested native model, so this section does not claim an independent model review.
