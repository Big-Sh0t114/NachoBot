# Local model device assignments

The setup wizard and Config Editor show graphics adapters detected on the computer running WebUI. NVIDIA adapters reported by nvidia-smi are selectable by stable GPU UUID. Windows CIM fallback entries are informational; a device name or PCI ID alone never makes an adapter selectable for CUDA.

TTS and Florence-2 VLM each have an independent choice. TTS is stored at [model_devices].tts in NachoBot-Multimodal-Adapter/configs/base.toml; VLM is stored at [perception.device].vlm in configs/perception.toml.

- Auto keeps inherited CUDA visibility and existing VLM fallback behavior.
- CPU masks CUDA for the managed process. For VLM, WebUI also sets NACHOBOT_VLM_DEVICE=cpu.
- NVIDIA GPU masks the process to the selected UUID. CUDA sees that card as logical cuda:0; VLM receives the matching logical-device override.
- Existing VLM values such as cuda:0 and cuda:1 remain supported with their original logical Torch meaning.

WebUI updates the TOML fields while preserving comments and unrelated settings. When an older install has webUI/data/model_devices.json but no canonical TTS field, it migrates valid choices once and archives the JSON as model_devices.json.migrated. Canonical TOML wins after migration. Missing config files are seeded from adapter templates when saving. Writes stage both TOML files and roll back a replaced file if the second write fails. Changes take effect when the matching managed service next starts or restarts; saving does not restart a running service.

If a saved GPU is missing, the panel shows it as unavailable so it can be changed to Auto or CPU. Starting a service with an unavailable explicit GPU reports an actionable error. The adapter TTS runtime manager and perception API read the same canonical TOML when launched outside WebUI. ASR continues to use CPU.
