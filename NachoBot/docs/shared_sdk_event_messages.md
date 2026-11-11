# Shared SDK event message input

The V2 SDK event wrapper accepts both NachoBot's shared `NachoMessages` event
object and older `MessageRecv`-like inputs. The wrapper waits for the shared SDK
plugin lifecycle to finish loading before it reads or copies the message. A load
failure, cancellation, or closed lifecycle prevents the event callback from
running.

## Native `NachoMessages`

The event callback receives a new dictionary containing the message's public
fields: `message_segments`, `message_base_info`, `plain_text`, `raw_message`,
`is_group_message`, `is_private_message`, `stream_id`, `llm_prompt`,
`llm_response_content`, `llm_response_reasoning`, `llm_response_model`,
`llm_response_tool_call`, `action_usage`, and `additional_data`.

`plain_text` is the canonical native text and is also copied to the compatibility
alias `processed_plain_text`. `raw_message` keeps its original value, including
`None`. The separate `stream_id` callback argument comes from the captured native
`stream_id`; a `None` value becomes an empty string. An empty-text context message
is still passed as a message, so prompt, response, scope, segment, and metadata
fields remain available without inventing a sender or ordinary user message.

The whole projected payload is deep-copied before the callback. Nested metadata,
message segments, and typed Python values stay detached from the input object;
they are not converted to JSON. Private implementation fields such as
`_modify_flags` are not exposed.

## Legacy `MessageRecv`-like inputs

The compatibility fallback starts with `raw_message`, `plain_text`, and
`processed_plain_text` set from `processed_plain_text`, matching the previous
adapter behavior. If a callable `to_dict()` succeeds, its result is applied with
`dict.update` precedence, including iterable pairs. Serializer errors leave the
fallback fields available. The callback stream argument continues to come from
`chat_stream.stream_id`; a serializer's `stream_id` key does not change it.
Legacy payloads are also deep-copied before dispatch.

## Callback return contract

The event callback still receives `message` and `stream_id` keyword arguments.
Tuple results are padded or truncated to five slots, and non-tuples keep the
existing default five-slot result. An explicit fifth-slot value is returned under
the existing contract. The host event pipeline applies only a `NachoMessages`
instance or `None` from that slot, so an ordinary dictionary is not a valid
replacement message. The adapter does not convert a dictionary into a
`NachoMessages` object or add a patch protocol. `None` remains `None`, including
lifecycle event calls such as start and stop.
