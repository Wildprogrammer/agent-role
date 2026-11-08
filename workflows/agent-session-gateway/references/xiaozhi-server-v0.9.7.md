# xiaozhi-esp32-server v0.9.7 integration

## Pinned upstream

- Repository: `https://github.com/xinnan-tech/xiaozhi-esp32-server`
- Release line: `v0.9.7`
- Commit: `87c6df77b0220ddbd8fc804f984f83f69f3225e5`
- License: MIT
- Deployment form: native Windows source checkout, not Docker, so the server can own a private stdio child gateway and reach loopback-only Agent Host APIs.

The installer rejects any other HEAD. It upgrades only exact hashes of prior
workflow-owned adapter versions; unknown existing bytes are rejected rather
than overwritten.

## Installed adapters

`scripts/install_xiaozhi_server_adapter.py` owns exactly four files in the
pinned checkout:

- `plugins_func/functions/hermes_bridge.py`: channel tools for Agent listing,
  switching, existing-session binding, ordinary bound dispatch, and confirmed
  lifecycle operations.
- `core/providers/llm/ollama_nonstream_tools/ollama_nonstream_tools.py`:
  deterministic `bound_agent` routing plus explicit local Ollama and remote
  OpenAI-compatible modes.
- `core/providers/tts/software_test_silence.py`: test-only valid silent WAV.
- `core/providers/tts/windows_sapi.py`: optional local Windows SAPI rollback.

The authoritative adapter sources live in this workflow. Files installed into
an upstream checkout are deployment copies.

## Runtime boundary

Use two isolated Python environments:

- Gateway environment: Python 3.11 or newer, installed from Agent Workflow Hub.
- Xiaozhi server environment: upstream-supported Python 3.10 with the pinned
  server requirements.

The server process receives the Gateway source path and reviewed configuration
through explicit environment variables. Do not install the Hub wheel into the
Python 3.10 server environment and do not merge the two dependency sets.

The reviewed stack uses local SenseVoiceSmall/FunASR ASR and SileroVAD. TTS is
an explicit operator choice; EdgeTTS needs network access, while Windows SAPI
is a manual local rollback. Model files, caches, voices, API keys, runtime YAML,
bindings, logs, and virtual environments are external state and are never
committed to this repository.

The normal device listeners are configurable LAN WebSocket and HTTP/OTA
endpoints. Agent Hosts and the control/MCP services stay on numeric loopback
addresses unless their own security contract explicitly says otherwise.
Examples use placeholders and never embed a maintainer LAN address.

## Software acceptance sequence

1. Clone the upstream repository and check out the exact commit above.
2. Create its Python 3.10 environment from the upstream pinned requirements.
3. Run the adapter installer in dry-run mode, review all four targets, then
   explicitly apply.
4. Run `doctor` against an external bridge configuration and confirm its
   SHA-256.
5. Exercise the private stdio plugin with
   `verify_xiaozhi_server_plugin.py`.
6. Start the managed server with all absolute paths passed explicitly to
   `start_xiaozhi_server.ps1`.
7. Use `simulate_xiaozhi_text.py` to verify Agent listing, stable binding,
   ordinary dispatch, lifecycle preparation, cancellation, and binding
   preservation before connecting physical hardware.

The production `bound_agent` path does not need a foundation-model API key:
unmatched ordinary text is sent to the explicitly bound Agent session. Local
Ollama and remote OpenAI-compatible modes are optional and never automatic
fallbacks.

Real microphone, speaker, wake-word, OLED, Wi-Fi, interruption, and power-cycle
claims require a physical-device acceptance run. Software simulation is not
evidence for those capabilities.

## 手机独立入口

受管启动器可通过 `-LingxiMobileConfig <ABSOLUTE_CONFIG_JSON>` 启用手机 HTTPS 入口，
复用当前 ASR/TTS 和网关宿主适配器；手机文字、照片和录音无需实体 ESP32 在线。
服务环境需要 `aiohttp>=3.13,<4`，Hub 测试环境使用 `.[dev]`，独立导入手机模块可选 `.[mobile]`。
完整依赖、证书生成、前台媒体边界和公网代理要求见 [mobile-client.md](mobile-client.md)。
