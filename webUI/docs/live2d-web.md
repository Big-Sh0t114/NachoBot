# Browser Live2D for voice calls

Browser avatars use an independent PixiJS renderer. They do not depend on the
desktop Live2D window, pygame, or the adapter's desktop websocket. Add
`live2d_web.router` to the WebUI FastAPI app; the existing WebUI HTTP auth
middleware then protects the model, asset, and Core routes.

The manager discovers `.model3.json` descriptors below
`NachoBot-Live2D-Adapter/resources` and other adapter directories. It excludes
virtual environments, caches, Git folders, and dependency directories. A
model appears only when its descriptor is valid and every declared moc,
texture, expression, motion, physics, pose, display-info, user-data, or motion
sound file is present and resolves within that model's directory. Model IDs
are stable hashes of normalized adapter-relative descriptor paths; display
names include the relative parent directory so duplicate basenames remain
distinct. New or changed bundles are rescanned within one second.

`GET /api/chat/live2d/models` returns `{available, reason, models}`. `reason`
is `ready`, `missing_core`, or `no_valid_models`; a missing Core does not erase
the discovered model list. Each model entry contains only its ID, display
name, same-origin descriptor URL, validated lip-sync parameter IDs, resolved
emotion expressions, and resolved canonical motion groups. The model route
revalidates its bundle before returning the entry. The asset route serves only
the descriptor and files declared in that model descriptor; it rejects
external references, traversal, undeclared files, and symlinks that resolve
outside the bundle.

Backend replies use the adapter's pure `ControlPipeline` and
`Live2DModelAdapter` helpers. `prepare_reply(call_id, model_id, raw_reply)`
returns `{reply, control_id}`; `apply_control(call_id, control_id)` returns a
bounded list of resolved expression and motion commands and applies each
control only once. Call pipelines are isolated and expire after an hour; the
backend should call `discard(call_id)` when a call ends. Plain text and
malformed structured replies are normalized as plain text with no avatar
commands.

The frontend uses `window.ChatLive2D.create({canvas, onFailure})`, then awaits
`load(modelEntry)`. Load readiness requires the locally served Cubism Core,
WebGL, a loaded Cubism model, runtime parameter enumeration, and a successful
render. Its result reports the actual runtime parameter IDs and the model's
verified expression and motion groups. `apply(commands)` filters each command
against those runtime groups, `setMouth(level)` clamps analyser values to
`0..1` and reapplies them after each model update, and `destroy()` releases the
model, renderer, resize observer, and window listener while retaining the
caller-owned canvas. A selected model that fails any browser load step must be
shown as an unavailable avatar; server model discovery alone is not browser
readiness.

## Pinned browser assets

The checked-in browser dependencies are pinned to these upstream releases:

| File | Upstream release | License | SHA-256 |
| --- | --- | --- | --- |
| `static/vendor/live2d/pixi-6.5.10.min.js` | PixiJS 6.5.10 | MIT; see `LICENSE-pixi.js.txt` | `403f2f2ee8145fa17f60c5c89403056efe2680e5096ec2762036486914ed19c5` |
| `static/vendor/live2d/pixi-live2d-display-0.4.0-cubism4.min.js` | pixi-live2d-display 0.4.0, Cubism 4 bundle | MIT; see `LICENSE-pixi-live2d-display.txt` | `af1267e6d52759b245766c578d905bfa025b532d5c3cc727c370957c4409e21b` |

Both packages are loaded from local static files. `ChatLive2D.load()` fetches
the Core through the authenticated same-origin API first, then loads the
Cubism 4 display bundle; do not add a static script tag for the display bundle
because it requires Cubism Core to have initialized first.

## Local Cubism Core dependency

Live2D publishes a versioned Cubism Core hosting file at
`https://cubism.live2d.com/sdk-web/core/05/live2dcubismcore.min.js` (the
official download page labels `/core/05/` as Cubism 5.2). Core is proprietary,
so it is intentionally kept outside Git at
`.runtime/webui-live2d/live2dcubismcore.min.js`; the ignored local file used
for this implementation has SHA-256
`25ae938cb4fe282ce189b357bcc97e603d1e1f7ec78bf04150d401c23cdc792f`.

For a new local checkout, obtain that official hosted file and place it at the
path above. The API reports `missing_core` until it exists. The WebUI serves
this local copy from `/api/chat/live2d/runtime/core.js` under its normal API
authentication. It is not included in the repository's static vendor files.
Live2D's license page identifies Cubism Core as proprietary and restricts
redistribution; this local setup does not assert general redistribution
rights. See the [Live2D Core hosting page](https://www.live2d.com/en/sdk/download/web/),
the [Proprietary Software License Agreement](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html),
and the [pixi-live2d-display documentation](https://guansss.github.io/pixi-live2d-display/).

## Local QA harness

`temp/voice-call-qa` is an ignored, isolated FastAPI harness. It mounts only
this router and the WebUI static tree, then shows discovered models, runtime
capabilities, expressions, motions, simulated mouth-level input, resizing, and
cleanup. It does not import or start `webUI/server.py` or any desktop adapter
process. Use a loopback port when starting it.
