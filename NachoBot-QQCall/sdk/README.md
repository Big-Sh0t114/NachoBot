# QQCall client

`nachobot-qqcall-client` provides the `qqcall_client` package shared by the
NapCat adapter, SnowLuma adapter, and QQCall plugin. It owns QQCall control
contracts, admission checks, adapter binding heartbeats, configuration access,
and the UniversalVC public API bindings.

The host application provides the generic `ncnk_message` message types and
transport. The message library has no dependency on QQCall or UniversalVC and
does not provide legacy import aliases for these clients.

This repository deploys the SDK as an editable local dependency. Its service
configuration helpers use the fixed sibling project layout to locate the
canonical plugin configuration and supervisor validators. Core, NapCat, and
SnowLuma declare both SDK locations in their `pyproject.toml` and `uv.lock`;
run their normal `uv sync --locked` after updating the checkout. Enabled QQ
adapters additionally use the existing `--extra qqcall-audio` switch.

Basic client imports require only lightweight HTTP and message dependencies.
PCM device support remains in the separate optional `nachobot-qqcall-pcm`
distribution. Recognition, VAD, and speaker processing remain in the
UniversalVC service process.

After migrating an existing running checkout, restart Core and the selected
QQ adapter normally so their Python module caches use the new imports.

## Container builds

Core and NapCat Compose files supply the sibling client sources as named build
contexts. Keep the repository layout when building either image. For direct
builds from the repository root, use:

```sh
docker build --tag nachobot-core:local --build-context qqcall=NachoBot-QQCall --build-context universalvc_sdk=NachoBot-UniversalVC-Adapter/sdk NachoBot
docker build --tag nachobot-napcat-adapter:local --build-context nachobot=NachoBot --build-context qqcall=NachoBot-QQCall --build-context universalvc_sdk=NachoBot-UniversalVC-Adapter/sdk NachoBot-Napcat-Adapter
```

The images copy only the lightweight client packages. NapCat also copies the
PCM package's `pyproject.toml` so `uv sync --locked` can validate the optional
dependency metadata; the `qqcall-audio` extra is not installed by these builds.
