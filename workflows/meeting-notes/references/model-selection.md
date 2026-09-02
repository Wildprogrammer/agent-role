# 本地模型选择

FunASR（ASR）与 VoxCPM（TTS/声音克隆）必须使用彼此独立的 Python 环境和模型缓存。二者的 Windows x64 / CPython 3.12 运行时均可由 Agent 按各自的哈希锁受控安装；VoxCPM 使用 CUDA 12.1 锁。两者的模型缓存都由使用者管理：每个模型目录都要记录官方来源、不可变 revision、许可证和本地文件 SHA-256 manifest；不要共享可变别名或让任一环境自动下载另一个模型。

两个分支都使用同一份可校验 manifest 结构：`revision` 为非空、不可变版本标识；`files` 为非空列表，每项含相对 `path`、`size_bytes` 和 64 位十六进制 `sha256`。doctor 会逐项核对路径、文件大小和 hash；不通过时不启动 worker。FunASR 的 `--vad-model` 与 `--punc-model` 若使用，也必须是已缓存的绝对本地目录，不能传模型 ID 或远端别名。

Always resolve an immutable model revision and save a hash manifest for the
downloaded snapshot. The model card license is independent from the FunASR code
license and must be accepted before download.

| Use case | Model IDs | Language scope | Hardware mode | Timestamp policy | Evidence required |
| --- | --- | --- | --- | --- | --- |
| Mandarin default | `paraformer-zh`, `fsmn-vad`, `ct-punc` | Mandarin Chinese | CPU allowed; CUDA optional after doctor | Retain provider timestamps when present; otherwise derive chunk boundaries and label them as chunk times | Exact IDs/revisions, model-card URLs/licenses, cache hashes, FunASR version, device, synthetic-audio smoke |
| Multilingual | `FunAudioLLM/Fun-ASR-Nano-2512` or a SenseVoice provider explicitly selected by the user | Only languages listed by the selected model card | Use a locally verified CPU/CUDA mode | Require a timestamp smoke test before claiming word/segment timestamps | Exact ID/revision, language list, model-card URL/license, cache hashes, device, synthetic-audio smoke |

Speaker diarization is disabled in v1 even if a provider exposes it. Do not set
`spk_model`, do not add speaker labels, and do not infer identity from voice.

Selection order:

1. Match the confirmed language.
2. Reject a model whose license or hardware requirements are not accepted.
3. Prefer local cached evidence with matching hashes.
4. If no verified provider fits, stop with `needs_dependency`; do not silently
   use a cloud API.

Official references:

- FunASR: https://github.com/modelscope/FunASR
- FunASR package: https://pypi.org/project/funasr/

## VoxCPM 语音模型

仅在用户明确请求语音输出时选择 VoxCPM。锁定 Python 包 `voxcpm==2.0.3`，模型使用一个官方来源的 `OpenBMB/VoxCPM2` 快照，并把实际不可变 revision 与本地缓存 manifest 传给 `doctor --with-voice` 和 `speak`。

| 用例 | 输入 | 额外门禁 | 输出 | 禁止项 |
| --- | --- | --- | --- | --- |
| 直接文字转语音 | 用户提供的非空文字 | 本地 Python、模型目录、revision、manifest 通过 doctor | `outputs/<run-id>/speech.wav` | 不启动服务、不写回 Obsidian |
| Vault 笔记转语音 | 用户明确选择的 Vault 相对路径，默认 `## 摘要` | 笔记路径与文件 hash 绑定 | 同上 | 不读取未指定笔记 |
| 声音克隆 | 上述输入加参考音频 | 本次明确授权、参考音频 hash、保留期限 | 同上，并写 provenance | 不得使用会议录音或参与者声音作为默认来源 |
