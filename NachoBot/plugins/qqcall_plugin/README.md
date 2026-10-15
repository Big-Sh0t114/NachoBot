# QQCall plugin

`NachoBot/plugins/qqcall_plugin/config.toml` is the fixed public QQCall
configuration. It uses schema 5 with `[plugin].enabled` as the only on/off
switch for the service, adapters, audio, and this Core plugin. The same file's
`[qqcall]` table holds the service settings and the Core behavior settings
`allow_planner_accept`, `group_intent_model_task`, and
`group_intent_timeout_seconds`. Core reads this canonical file
when it starts; changing `plugin.enabled` requires restarting Core to refresh
command and Planner-action registration. Command and action invocations also
reread the switch before dispatch and fail closed if it has been disabled.
Missing or invalid canonical TOML disables plugin registration. The plugin
does not generate a plugin-local config or use a local copy. It sends requests
through Core's typed `qqcall_request` API and the shared QQCall Runtime; it does
not open another QQ socket or call a vendor API directly.

In a verified QQ private chat, `#stat_vc_qq` queries the current call state.
The text commands `#qqcall accept`, `#qqcall hangup`, and `#qqcall reject`
have been removed from registration and command dispatch. Automatic incoming
call handling and the optional event-bound Planner accept action remain
available under their existing policies; internal call-control APIs are unchanged.

Command replies use the same light, friendly NachoBot voice as the music
commands, with concise Chinese messages and occasional “喵”. A confirmed join
says “来啦，已经加入群语音了喵~”; a confirmed leave says
“已经离开群语音啦，需要时再叫我喵~”. Unknown results remain explicitly
unconfirmed, missing dependencies are named, and denied peers/groups remain
silent. State queries use multiple lines and do not treat an invitation or a
room observation as proof that the bot joined.

The group controls are `#join_vc_qq`, `#leave_vc_qq`, and `#stat_vc_qq`.
The former `#qqcall join/leave/status` spellings are retired. Voice-call messages
declare Chinese TTS through their per-message capabilities; the ordinary TTS
plugin default is unchanged. Private speech uses the current adapter connection
to look up the caller's real QQ nickname, checks the returned QQ identity, and
falls back to the QQ number if lookup fails.

Only `#join_vc_qq` (including the existing voice transcript wrapper),
`#leave_vc_qq`, and `#stat_vc_qq` are registered as commands. The old fixed
phrases `请加入群语音`, `加入当前群通话`, and `加入群语音` are not registered.
Internal semantic-review helpers are not a separate natural-language command
entry point. Group requests still require matching live QQ group and member
identity, message provenance, and the adapter's current admission decision.

The adapter remains the admission authority and applies its existing per-user,
per-group and global policies. Core has no independent QQCall user list. The
semantic judge does not establish room availability or membership. An occupied
global call slot is reported as busy. The command reports a successful join only after the typed response confirms
`joined=true`, the verified native source, the same requester, and the same
group. A leave requires `left=true`. Unverified or stale results never produce
a success reply.

Planner acceptance is separately disabled by default. Setting
`[qqcall].allow_planner_accept = true` registers `qqcall_accept`. That action
only uses a senderless `qq.call.invite_callback_ready` event whose validated
private route matches the current private chat stream, whose full
`qqcall_scope` is valid, and whose event reports a ready callback, a positively
matched invite correlation, and no uncertainty. Any scope values supplied by
the model must match the event scope in full.

The plugin provides no audio capture or playback feature and does not enable
automatic answering.

The shared service resolves audio names from the active endpoint IDs already
pinned by its private native route guard. `[qqcall.audio]` may be omitted or
may contain only `capture_device` and/or `playback_device` as exact-name
overrides. The service rejects ambiguous, inactive, replaced, or unrecognized
virtual endpoints before launching Host; it never chooses a Windows default or
an unrelated microphone. This automatic route discovery is shared by NapCat
and SnowLuma, and has no separate live-call acceptance claim.
