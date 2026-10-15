# AVSDK loader/export probe

This probe checks one pinned Windows AVSDK DLL in a short-lived child process. It verifies the SHA-256 and AMD64 PE headers in both parent and child, loads the DLL with `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32`, then resolves exactly `StartToDumpAudio`, `StopDumpingAudio`, `PPP_InitializeModule`, `PPP_GetInterface`, and `PPP_ShutdownModule`. It reports only whether each export resolved and its RVA. It never calls an export.

Run it only against the explicitly selected DLL and a new, empty output directory:

```powershell
& 'C:\Python312\python.exe' .\loader_probe.py `
  --dll 'C:\path\to\verified\AVSDKPlugin.dll' `
  --output-dir 'C:\path\to\fresh\avsdk-probe-output' `
  --timeout-sec 10
```

The accepted DLL SHA-256 is `757C48DDC0B5FDFF101C36BF309BF9F79FE8618A64FBF0D059347AD3834A9DBF`; the required PE machine is AMD64. The expected RVAs are `StartToDumpAudio=0x32FFC0` and `StopDumpingAudio=0x32FFE0`. A report with `status=pcm_unverified` and `loader_metadata_resolved=true` means only that all five exports resolved and those two RVAs matched. `pcm_verified` is always false. Hash, architecture, load, missing-export, RVA, timeout, and child failures retain fixed error codes; a DLL-load failure may include a numeric Win32 error code, never its native error text.

The child gets a dedicated working directory, temporary directory, and profile directories under the chosen output directory, plus a minimal Windows environment. This is process/environment isolation, **not a filesystem sandbox**. Loading a DLL runs its `DllMain` and dependency initialization even though this tool never calls an export; that implicit behavior cannot be suppressed here. The known audio-dump routines are specifically excluded because their effects are not suitable for a metadata probe.

Static analysis of the pinned DLL found that `StartToDumpAudio` at RVA `0x32FFC0` reaches `ForceToDumpAudio` at `0x361C30`, which enables `dump_record_audio`, `dump_after_3A_audio`, `dump_before_encode_audio`, and `dump_playout_audio`. `StopDumpingAudio` at `0x32FFE0` reaches `ResetConfig` at `0x361C80`, which re-reads `Desktop\qq_audio_debug_config.json`; the analysis does not establish that this clears or disables every setting. A helper at `0x3D7930` derives `TRAE_DBG_DUMP` from `CSIDL_DESKTOPDIRECTORY` and directory APIs. Windows can redirect that special-folder path, so it is not necessarily `%USERPROFILE%\Desktop`, and overriding the child `USERPROFILE` does not establish where it resolves. The internal writer at `0x44C4D0` opens append-mode (`ab`) candidate files named `after_jitter_buffer_%02d.pcm`. These are static findings only; the file format, real-time behavior, and completeness of the read/write path have not been verified by running the DLL.

Static PPAPI inspection found `PPP_GetInterface` at VA `0x1802C7390` registering `PPP_Instance;1.1` and `PPP_Messaging;1.0`; VA `0x1812C3000` is the `PPP_Messaging` interface table. A message callback observed at `0x180299CF0` parses `PP_Var` dictionaries/arrays and dispatches through a slot at `0x18137C140`. The slot is written at `0x18029A39F`, and initialization near `0x1802C7487` passes dispatcher `0x18029DBF0`. The dispatcher is table-driven: one table at `0x18137C630` covers commands 0–111 with 16-byte entries, and another at `0x1812C3010` covers 100000–100043 with 8-byte entries. No entry was mapped to a raw-PCM command or callback. `PPB_VarArrayBuffer;1.0` and `OnAudioFrameOut` appear in strings/logging, but that alone does not establish such a path. Failure to find it is not proof that no such path exists.

This probe does not initialize the PPAPI module, create a renderer or listener, invoke `StartToDumpAudio`/`StopDumpingAudio`, capture or play audio, launch QQ, or inject into another process. The included tests use synthetic PE files and mocked Win32 APIs; they do not load or execute the vendor DLL. No upstream probe implementation is copied into this tool.

## One controlled loader run

At 02:18:43, the parent ran the pinned probe once with a 10-second timeout. `child-result.json` recorded all five exports resolved, the expected dump export RVAs, and stages `dll_loaded` / `export_resolved`. The child then exited with `3221226505` (`0xC0000409`); Windows Application Error 1000 named `AVSDKPlugin.dll` as the faulting module at offset `0xE3CB31`. The final `report.json` correctly reports `child_failed` / `child_exit_nonzero`, with `pcm_verified=false` and no timeout. The partial child metadata does not make the overall probe successful.

DLL loading necessarily ran `DllMain` and dependency initialization, but none of the five AVSDK exports was called. There was no QQ injection, login, or signaling, and this run did not verify PCM capture or playback or establish a usable standalone clean shutdown. The loader-child crash does not invalidate the separate prior in-process PPAPI Host validation. The fault cause remains under static review; this note does not infer a root cause.
