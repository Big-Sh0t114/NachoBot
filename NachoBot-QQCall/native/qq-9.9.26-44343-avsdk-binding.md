# QQ 9.9.26-44343 AVSDK listener binding evidence

Read-only analysis of `C:\Program Files\Tencent\QQNT\versions\9.9.26-44343\resources\app\wrapper.node` (97,233,448 bytes; SHA-256 `29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2`). The hash identifies the on-disk file analyzed; it does not independently attest to the bytes in a live mapping.

## Binding path

The x64 PE has image base `0x180000000`; `.text` maps RVA `0x1000` to raw offset `0x400`, and `.rdata` maps RVA `0x34f6000` to raw offset `0x34f5200`.

The N-API method descriptors are in `.rdata`:

| Method | Descriptor raw offset | Callback VA |
| --- | ---: | ---: |
| `addKernelAVSDKListener` | `0x368e400` | `0x180dec5bc` |
| `removeKernelAVSDKListener` | `0x368e440` | `0x180dec7b2` |

Both callback bodies call `napi_get_cb_info` through IAT VA `0x183e3e760`, set the requested argument count to one, then call `napi_unwrap` on the service receiver through IAT VA `0x183e3e818`. Their assertion strings independently state `argc == 1`.

Both callbacks pass the sole argument through the same converter (`0x180dec774`; add callsite `0x180dec66c`, remove callsite `0x180dec862`). The converter reaches `0x181dd05c4`, which retains the received `napi_value` by calling `napi_create_reference(env, value, 1, &ref)` through IAT VA `0x183e3e720`. The inspected conversion path does not extract a string or integer ID, nor read a named property such as `getId`.

After conversion, add dispatches through the unwrapped service vtable at offset `+0x8`; remove dispatches at `+0x10`. Both N-API callbacks end with `xor eax,eax; ret`, so add returns no `napi_value` handle (consistent with the observed JavaScript `undefined`). The prior assumption that add returns a removal ID is disproved.

## Contract boundary

The wrapper evidence supports passing a JS value of the same retained-reference type to both add and remove. Reusing the exact listener object is the best-supported removal argument. The binary inspected here does not establish how the underlying service compares listener references or confirm that remove affects only the matching registration; that behavior remains unverified. No removal was attempted against a live QQ process.

## Bounded private-invite evidence for this on-disk build

The SHA-256 above is the sole allowlisted build for the optional private-scope profile. Bootstrap derives that profile only after hashing the wrapper file found through the actual load/cache path and matching the exact digest. This is a file-hash gate; it does not attest to bytes in a live memory mapping. No user or configuration value can enable the profile.

The known C2C path in this build supports a narrow `invite_type=1` candidate:

1. The wrapper converter at `0x181dd189e`–`0x181dd19c9` exports `relation_id` from native offset `+0` as a string, `invite_type` from `+8` as an int32, and `from_uid` from `+0x10` as a string. It does not export `relation_type`.
2. `HandleS2CInvitePush` at `0x18135a686` handles command `0x158`, checks input type `3`, and matches `QRTC_C2C_CMD_S2C_ONLINE_PUSH` at VA `0x1837000a0`. That branch reaches the C2C producer at `0x18135c646`; it sets `R8D=1` at `0x18135c745`/`747` before calling the invite constructor at `0x18135c750`. The constructor stores that value at object offset `+8` (`0x18135c437`).
3. The event continues through `0x18135c588`, event-bus calls at `0x181363a46`/`0x181363ada`, and the listener thunk at `0x180019130`. The `KernelAVSDKService` `IAVSDKNotifyAPI` subobject is at offset 16; its vtable slot `+0x20` is `0x1804e8b8e`. The callback reaches the Node listener at `0x181dd0774`, dispatcher `0x181dd16b3`, and converter `0x181dd1710`; `OnInviteActionToAVSDK` is named at `0x181dd17b5` and invoked with three arguments at `0x181dd1806`. The copied `+8` value remains in the inspected path at `0x1804e8bc9`/`0x1804e8cab` and `0x18052de22`/`0x18052de25`; the Node listener vtable entry is `0x1838477d8 + 0x10 = 0x181dd0774`.

The exact known C2C producer uses `invite_type=1`, and its `from_uid` constant at VA `0x183e353b6` maps to an empty string. The Agent therefore accepts only an own, data-backed literal empty string or a bounded nonempty string for this field; missing, null, accessor-backed, malformed, and other-type values do not produce native evidence. Empty `from_uid` can be correlated only when `relation_id` is the canonical peer UIN in the current pinned UID map and the same UID matches SDK `20006`. Nonempty `from_uid` must itself equal the mapped peer UID and the `20006` caller.

The separate MAV subtype `0x149` follows `rpc.qqrtc.mav_appsvr.MavAppsvr.SsoS2CInvite` to `0x18135a9fc`; its observed conditional maps `relation_type == 3` to invite type 2 and other values to type 3. The semantics of type 2 and type 3 remain unknown, so neither is classified as private. This is a bounded known producer chain, not an exhaustive whole-binary audit, proof of the live mapped image, capture of an actual caller's invite type, or verification of a production call. No live call was performed for this classification.

## Reproduction

Use `objdump -p wrapper.node` to map the N-API IAT slots, then disassemble the callback/converter ranges with `objdump -d -M intel --start-address=0x180dec5bc --stop-address=0x180dec774 wrapper.node` and `objdump -d -M intel --start-address=0x180dec7b2 --stop-address=0x180dec938 wrapper.node`. The reference wrapper is at `0x181dd05c4`.
