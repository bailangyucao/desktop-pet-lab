# Desktop Pet Lab

A Windows desktop companion learning project developed with **Codex through vibe coding**, using Python, Tkinter and Pillow.

[中文说明](README.md)

Features include a compact reply bubble with a separate input strip below the character, animated sprites, a streaming chat bubble attached to the character, configurable persona and memory, OpenAI-compatible Chat Completions endpoints, local conversation history, and Windows user-bound API-key encryption.

## Development disclosure

This is my attempt to learn desktop application development with Codex. I have not yet systematically surveyed existing open-source desktop-pet projects, and I do not claim that the features, interactions or implementations are novel.

If you find similarities or duplication, please open a similarity/attribution issue with the project URL and specific evidence. I will investigate and add acknowledgements, source information and any necessary licensing clarification, and adjust or remove relevant content where appropriate. This statement does not replace upstream licensing obligations.

See [AI_DEVELOPMENT.md](AI_DEVELOPMENT.md), [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md), and [ASSET_PROVENANCE.md](ASSET_PROVENANCE.md).

## Code-only distribution

The provenance and redistribution permission of the original character artwork have not been confirmed. This repository therefore omits that artwork, its icons, the original executable and binaries embedding it. The MIT license applies to this project's code and documentation, not that artwork or third-party dependencies.

To run the GUI, provide your own authorized transparent sprite sheet at `assets/spritesheet.png`. See [the sprite contract](assets/README.md). Core tests do not require artwork or API credentials.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -v
# After supplying authorized artwork:
.\.venv\Scripts\python.exe desktop_pet.py
```

Double-click the character to chat; drag to move; right-click for actions and settings. Configure your endpoint, model and API key before sending a real request. Model-service availability and account permissions require your own verification.

The Windows build script is `build.ps1`. The public code-only test run passes 17 core tests and skips 6 GUI tests without artwork. With local artwork, v2.0.4 passed all 23 tests and an executable startup smoke test. CI has been prepared but has not yet run on GitHub.

Do not commit API keys, `.env`, `data/`, conversation history or logs. Contributions and source/attribution corrections are welcome.

Release materials: [initial release](docs/RELEASE-v2.0.4.md), [validation](docs/VALIDATION.md), [third-party notices](THIRD_PARTY_NOTICES.md).
