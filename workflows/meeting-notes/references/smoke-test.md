# Opt-in meeting workflow smoke test

## Preconditions

- Obtain explicit approval.
- Use non-sensitive synthetic audio; never record a real meeting for testing.
- Never test VoxCPM cloning with a real participant recording or an unapproved reference voice.
- Record OS, architecture, FFmpeg/FunASR/model versions, model
  revision/license, artifact hashes, and adapter evidence.
- Create a temporary Vault under the test workspace.

## Procedure

1. Generate or import a 30-second synthetic Mandarin clip with known text.
2. Normalize a copy with the locked FFmpeg and record the source/output hashes.
3. Transcribe locally with diarization disabled; confirm no `speaker` field
   exists.
4. Interrupt after one chunk, resume, and prove only missing/failed chunks
   rerun.
5. Verify `[听不清 HH:MM:SS]` survives the merge.
6. Select transcript-only and confirm no `meeting-notes.md` or Vault write
   occurs.
7. Resume, approve local summarization, and write to the temporary Vault in
   `new` mode.
8. Exercise append, overwrite-without-approval, approved overwrite, path escape,
   and unwritable-Vault cases.
9. If remote summarization is tested, use disposable text and obtain separate
   approval naming the provider and destination.
10. Run the fake-dependency VoxCPM worker and failure-cleanup tests (`pytest workflows/meeting-notes/tests/test_voice.py workflows/meeting-notes/tests/test_cli.py -q`), and confirm the worker writes only the requested WAV, no service code is present, and a worker failure removes the new output directory.
    Also confirm long text is chunked losslessly at punctuation with a single model load, and that a worker timeout returns `processing-failed` and removes the new output directory.
11. If a user-managed VoxCPM environment is available, use only disposable direct text and a synthetic/local test model to run one local TTS smoke. Confirm the output remains under `outputs/<run-id>/`, no listener is started, and no Obsidian mutation occurs.
12. Save sanitized evidence without credentials, private transcript content, or
    user-specific absolute paths.

## Result

Mark only the tested host/capability/model/system tuple as verified. Real
microphone capture remains unverified unless separately approved and tested
without retaining private audio.

## Cleanup

Dry-run and remove only the run workspace and temporary Vault after approval.
Never delete original media or an external Vault.
