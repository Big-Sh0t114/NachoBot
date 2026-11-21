# Local model device assignments

The setup wizard's second step and the Config Editor's **模型运行设备** panel show the graphics adapters detected on the computer running WebUI. NVIDIA adapters reported by `nvidia-smi` are selectable by their stable GPU UUID. Windows CIM fallback entries require a valid physical PCI device ID; ROOT, software, USB, malformed, and known virtual PCI display devices are excluded. Physical Intel, AMD, NVIDIA, and other PCI adapters can appear as detection-only entries unless `nvidia-smi` validates them. A device name or PCI ID alone never makes an adapter selectable for CUDA.

TTS and Florence-2 VLM each have an independent choice. The TTS choice is stored at `[model_devices].tts` in `NachoBot-Multimodal-Adapter/configs/base.toml`; the VLM choice is stored at `[perception.device].vlm` in `configs/perception.toml`.

- **自动** keeps inherited CUDA visibility. For VLM, it uses the existing CUDA/CPU fallback behavior.
- **CPU** masks CUDA for that managed process. For VLM, WebUI also sets `NACHOBOT_VLM_DEVICE=cpu`.
- **NVIDIA GPU** masks the process to the selected UUID. CUDA sees that card as logical `cuda:0`; VLM receives the same logical device override.
- Existing VLM values such as `cuda:0` and `cuda:1` remain selectable and keep their original logical Torch meaning.

The root `launchbot.bat` and `launchbot_lite.bat` derive their Python environments from the selected TTS engine and these model-device fields. Local TTS on CPU uses the CPU environment; a selected NVIDIA UUID uses the CUDA environment; Auto selects CUDA when an NVIDIA GPU is available and visible to the launcher, and CPU otherwise. VLM Auto follows its existing runtime behavior, including a valid inherited `NACHOBOT_VLM_DEVICE` override and CPU fallback when CUDA is unavailable. FULL starts TTS and perception with independent profiles, so CPU TTS can run beside GPU VLM and vice versa. GenericHTTP uses a small HTTP-facade environment and ignores the local TTS device choice. LITE never reads the perception device file and prepares only the TTS environment. There is no separate BAT runtime selector.

The WebUI edits these TOML fields while preserving comments and unrelated settings. If an older install has `webUI/data/model_devices.json` but no canonical TTS field, the WebUI migrates its valid choices once and archives the JSON as `model_devices.json.migrated`. Canonical TOML always wins after that. Missing files are seeded from the complete adapter templates when saving. Writes stage both TOML files and roll back a replaced file if the second write fails. They take effect the next time the matching service starts or restarts; saving never restarts a running service. If a saved GPU is missing, the panel keeps showing it as unavailable so it can be changed to Auto or CPU; an attempt to start with a missing explicit GPU reports an actionable error.

The adapter runtime manager and perception API read the same canonical TOML when launched outside WebUI. Selecting a GPU while explicitly launching the CPU Multimodal runtime is rejected with a clear error. ASR currently uses CPU through the standard `sherpa-onnx` runtime; CUDA ASR installation is not provided here.

Hardware recommendations continue to use the largest single NVIDIA GPU. VRAM is not added across cards, and the recommendation does not change the saved per-model assignments.
