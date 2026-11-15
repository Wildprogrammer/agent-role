# Third-party notices

Agent Workflow Hub depends on or interoperates with third-party projects. Their
licenses remain their own; the Hub MIT License does not replace them.

## Agent Session Gateway

- **xiaozhi-esp32-server** — <https://github.com/xinnan-tech/xiaozhi-esp32-server>
  — pinned integration commit
  `87c6df77b0220ddbd8fc804f984f83f69f3225e5` — MIT License. The Gateway
  distributes modified adapter files intended for installation into this
  upstream project; retain the upstream copyright and license when
  redistributing substantial portions.
- **Model Context Protocol Python SDK** —
  <https://github.com/modelcontextprotocol/python-sdk> — runtime dependency;
  not vendored by this repository.
- **Hermes Agent** and **DeepSeek Harness** — optional external Agent Hosts;
  not vendored or redistributed by this repository.
- **Lingxi ESP32 firmware** — maintained and attributed separately at
  <https://github.com/Wildprogrammer/lingxi-esp32>. Firmware binaries may carry
  additional component and model notices that are not covered by this file.

Python package licenses are resolved from `pyproject.toml` by the installer.
Release automation should generate an SBOM and verify dependency licenses for
the exact locked environment before publishing binary artifacts.


### Optional Lingxi pet videos

The optional installer retrieves unmodified animation assets from
[dsh-pet 0.4.7](https://github.com/PC2005-cloud/dsh-pet).
The character and these assets are subject to
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
The original character 溟月 is by 上善无形; the DeepSeek-style design is by
ZipZipPipe; the dsh-pet animation project is maintained by PC2005-cloud.
See the [Gateway attribution guide](workflows/agent-session-gateway/references/companion-pet.md)
for source links and the fixed manifest for individual clip provenance.
