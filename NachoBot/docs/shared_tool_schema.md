# Shared tool input schemas

`build_tool_input_schema` in `src/llm_models/payload_content/tool_option.py` is the shared encoder used by both provider converters. It translates legacy `ToolParam` declarations into standard JSON Schema types: strings remain `string`, integers `integer`, floats become `number`, and booleans become `boolean`. Descriptions, declaration order, required names, and nonempty enum values are preserved. Enum values keep their JSON scalar types.

An explicit `ToolOption.input_schema`, including an empty object, is authoritative and is deep-copied without rewriting or validating its contents. Tools with no raw schema and no parameters continue to omit the provider parameter schema. Returned schemas are detached from the source option and from each provider conversion.

OpenAI receives the shared schema in the function `parameters` field, following its [function calling guide](https://developers.openai.com/api/docs/guides/function-calling). Gemini receives every non-`None` shared schema through `parameters_json_schema`; the SDK exposes this JSON Schema field alongside the mutually exclusive `parameters` field in its [FunctionDeclaration reference](https://aistudio.google.com/api-reference/caching#FunctionDeclaration). The field is present in the supported SDK declaration from [google-genai v1.39.1](https://github.com/googleapis/python-genai/blob/v1.39.1/google/genai/types.py#L2099).

This documents client-side payload construction only. Acceptance by a particular provider endpoint or proxy has not been verified here.
