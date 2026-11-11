# Shared tool result outcomes

Plugin tools can explicitly mark a failed observation by returning a mapping whose
`type` is the exact string `tool_error`. The shared reply renderer also recognizes
the existing MCP marker `mcp_tool_error`. The plugin executor exposes either marker
to callers as canonical `tool_error` status while preserving the selected content.

Status is opt-in. Legacy output without one of those exact type values remains a
successful `function` result, even when it contains failure prose, `success: false`,
an `error` field, or another status field. The host does not infer failure from
tool names or content. An empty producer result continues to mean no result.

Known producer adoptions are the disabled and exception paths of the built-in LPMM
knowledge search, plus the exception path of the diary `EmotionAnalysisTool`. Their
content and the valid empty-knowledge result are unchanged. Other plugins and SDK
adapters can opt in by returning the reserved type explicitly; `BaseTool.execute`
and `direct_execute` continue to return the producer mapping unchanged.

The LLM-facing executor wraps the chosen producer value in its existing tool
envelope. It uses `content` when present, otherwise `error` when present, otherwise
the mapping text. It does not add producer payload fields or mutate the mapping.
It snapshots explicit status before converting the selected content because
producer-defined string conversion can run code. The executor does not retry a tool
or roll back business operations that may already have had side effects.
Batch execution retains explicit failures for inspection but does not report them
as successful tools or cache a bundle containing them. The simple direct execution
convenience returns `None` for an explicit failure; `execute_tool_call` returns the
status-bearing envelope.

The shared renderer labels explicit plugin and MCP errors as failed observations and
preserves their raw content under the existing size limits. Knowledge prompt paths
omit explicitly failed observations. Untagged legacy outputs keep their prior
rendering behavior.

The plugin tool cache scope uses an `outcomes-v1` namespace so results stored before
explicit outcome normalization are not reused under the new contract. Cacheability,
catalog eligibility, TTL, context sensitivity, and complete-attempt checks are
otherwise unchanged.

## Verification

The isolated unittest matrix was run under Python 3.12.11 and 3.11.14. From
`NachoBot`, run each pattern with the corresponding interpreter:

```powershell
$python = './.venv/Scripts/python.exe' # Repeat with the local Python 3.11.14 executable.
$patterns = @('test_tool_results.py', 'test_tool_execution_policy.py', 'test_result_cache_lifetime.py', 'test_tool_arguments.py', 'test_tool_cache_policy.py', 'test_mcp_tool_executor.py', 'test_reply_tool_prompt_prose.py', 'test_prompt_rendering.py')
foreach ($pattern in $patterns) { & $python -m unittest discover -s tests -p $pattern -q }
```

The matrix passed 115 tests per interpreter. It exercises production declarations
through isolated AST loaders and fake dependencies; it does not load a live model,
knowledge backend, database, or service.
