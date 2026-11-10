# Shared tool context budgets

`build_reply_tool_context` bounds the rendered webpage, search, extracted-page,
and tool evidence block to 16,000 characters. Each section receives a fair share
of the available evidence space, with unused space redistributed; one section
can use at most 8,000 characters. Introductory prose, section labels, separators,
and the closing instruction count inside that evidence budget. A large first page
therefore leaves room for later search and tool results.

The group and private replyers keep their established short-output wording.
The older context builder uses the same natural-language evidence format. Tool
content, search text, and page text remain in their original result structures;
only the strings sent to the model are bounded. The wrapper does not expose
internal tool names or result types in replyer prose.

The pending sandbox confirmation instructions and candidate query are appended
after the bounded evidence block. They retain their full text and candidate
identity, so this evidence budget does not cap the complete reply prompt.

Each MCP observation is bounded independently by
`MCPToolExecutor.observation_max_chars`, including its round header, untrusted-data
notice, result metadata, closing tag, and next-call instruction. The existing
setting is clamped to 1,000 through 50,000 characters. MCP tool execution,
permissions, and result retention are unchanged. The base prompt and tools sent
to each MCP model call are outside this observation limit; this is not a global
prompt or token budget.

Both paths use the standard-library renderer in
`src/common/prompt_rendering.py`. Top-level short strings remain byte-for-byte
unchanged. Builtin mappings and sequences render as readable JSON-like values,
with visible truncation and omission markers. The renderer limits nested depth,
visited values, section count, and label length; it never calls arbitrary object
`str` or `repr` methods. Raw tool results remain available to programmatic
consumers and are not mutated.
