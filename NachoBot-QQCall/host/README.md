# Shared PPAPI AV Host

The Host is a small hidden Electron renderer that loads QQ's PPAPI AVSDK plugin from the explicitly selected QQ `--app-path`. Its HTTP server is independent of NapCat and SnowLuma. It does not attach a Kernel listener, implement call policy, log in, or create audio streams.

The Host PID is a dedicated QQ process selected for PPAPI rendering. It must not be confused with the bot QQ PID used by the separate Kernel Agent. Keep the Host on its own `userData` profile. The Host Shim configuration uses its own `hostGeneration`; the Agent's `connectionGeneration` is a separate connection space and is not compared by the Host.

## Local HTTP contract

The server binds only to `127.0.0.1`. Every route requires `Authorization: Bearer <ipcToken>`, including `GET /healthz`. Token comparison is timing-safe. Health returns sanitized status metadata; it never includes the token, login strings, IDs from the AVSDK payload, payload contents, or exception stacks. HTTP and serialized plugin-output frames are limited to 1 MiB.

`GET /healthz` returns HTTP 200 only after the renderer reported both `pluginFound` and `hasPostMessage`. Before readiness it returns HTTP 503 with `ok:false`; a renderer crash, a stopped Host, or a failed output path also leaves the Host unavailable.
The JSON response is `{ok, status}`. `status` contains only `hostGeneration`, readiness/plugin booleans, `stopped`, `listening`, loopback/port metadata, invoke/output counters, bounded-queue size, `outputFailed`, and a safe `lastFailureCode`.

`POST /v1/invoke` accepts exactly this JSON shape:

```json
{"command":1,"id":1,"params":["uid","uin","uin","account path",""],"hostGeneration":1}
```

`id` is a positive safe integer supplied by the controller. It is preserved unchanged. `hostGeneration` is a positive safe integer fixed for the Host run and must match exactly. The Host accepts only these verified command signatures:

- Command `1`: five strings, each no larger than 8 KiB.
- Command `5`: `[finite number, nonempty string, [one nonempty string], finite number, string, boolean, string]`; each string is no larger than 8 KiB.
- Command `55`: `[finite number, string]`; the string is no larger than 256 KiB.

The renderer calls `plugin.postMessage({cmd: command, id, param: params})`. HTTP success is exactly `{posted:true,id,command}` and is sent only after that call returns. It does not mean the AVSDK accepted the command or produced a native response. Invalid requests return a typed 4xx/5xx code. The Host never retries a command.

PPAPI output messages are serialized in arrival order and POSTed to the controller at `http://127.0.0.1:<outputPort>/v1/avsdk/output` as `{command,id,value,hostGeneration}` with the same Bearer token. The controller should return a 2xx response only after accepting the event. The Host applies a bounded queue and a write timeout; a queue overflow or forwarding failure marks the Host unavailable, drops pending output without retries, and reports only a safe failure code.

## Private config and startup

`scripts/host-config.cjs` writes two private JSON files: the native-compatible Shim config (default `<reportPath>.host.bootstrap.config.json`) and `<reportPath>.host.config.json`. The sidecar repeats the bootstrap token and generation; `host/bootstrap.cjs` reads it and checks exact token, generation, module, app path, report path, pipe, session, selected Host PID, and loopback agreement before starting. The Host generation equals this Host Shim config's `connectionGeneration`. The generated token is also the Host HTTP Bearer token and must be passed privately to the Bridge. Never print or commit either JSON file.

The config command only writes files. It does not discover or start QQ, launch/inject a process, or register a listener. Select the dedicated Host PID and its resolved running QQ `--app-path` manually; use the same Host PID, app path, generation, and report base when generating the config. The profile directory must already exist.

The bootstrap and standalone Electron entry best-effort write lifecycle diagnostics to `<reportPath>.host.status.json`. Each atomic report contains only `phase`, `hostGeneration`, `targetPid`, `lastFailureCode`, `ready`, `listening`, and `stopped`; it records initialization, startup failures, ready state, renderer/output failures, and shutdown. Report write errors are ignored and do not change Host ownership. The report contains no token, config, filesystem path, payload, or stack.

For a dedicated Host QQ launch, select the executable and app path explicitly. Prepare a separate profile and pass the PPAPI switches on the process command line before Electron starts; the selected app path must be the one that owns the plugin binary. This is a manual operator command, not something the Host config CLI runs:

```powershell
$qqExe = 'C:\Path\To\Selected\QQ.exe'
$qqAppPath = 'C:\Path\Reported\By\The\Selected\Host\QQ\--app-path'
$profilePath = Join-Path $env:LOCALAPPDATA 'QQCallHostProfile'
$reportBase = Join-Path $PWD 'NachoBot-QQCall\probe\.runtime\av-host'
New-Item -ItemType Directory -Force -Path $profilePath | Out-Null
$pluginPath = Join-Path $qqAppPath 'avsdk\AVSDKPlugin.dll'
$hostArgs = @(
  "--user-data-dir=$profilePath",
  '--allow-command-line-plugins',
  "--register-pepper-plugins=$pluginPath;application/x-ppapi-avSDK"
)
Start-Process -FilePath $qqExe -ArgumentList $hostArgs
```

After launch, use the existing injector's `--discover` output to select that exact dedicated Host process and copy its fresh PID and `createdUtc`. Then write the private configs for that selected process:

```powershell
$hostPid = [int](Read-Host 'Host PID from fresh --discover output')
$hostCreatedUtc = Read-Host 'Host createdUtc from fresh --discover output'
node .\NachoBot-QQCall\scripts\host-config.cjs `
  --pid $hostPid `
  --app-path $qqAppPath `
  --report $reportBase `
  --profile-path $profilePath `
  --listen-port 6111 `
  --output-port 6110 `
  --generation 1
```

After reviewing the target identity, the existing injector accepts the generated native config. Its explicit `--pid` and `--expected-start` must refer to the dedicated Host QQ process, not the bot QQ PID or a personal QQ process:

```powershell
$shimConfig = "$reportBase.host.bootstrap.config.json"
python .\NachoBot-QQCall\scripts\qqcall_injector.py `
  --inject `
  --pid $hostPid `
  --expected-start $hostCreatedUtc `
  --config $shimConfig
```

A successful dry run, health response, or device-selection response does not establish live Electron/PPAPI behavior, QQ injection, multi-instance behavior, PCM routing, or remote call audio.

## Acceptance boundary

Live Electron/PPAPI startup, dedicated QQ launch, injection, multi-instance behavior, and call audio require separate target-runtime verification.

Attribution: The PPAPI element, plugin path, and command-posting behavior were informed by a locally supplied Windows validation probe. The original files are not redistributed and have no license header. This Host is a clean rewrite; it does not redistribute the probe files or bundle third-party GPL code. Confirm the probe owner's licensing terms before redistribution outside this project.
