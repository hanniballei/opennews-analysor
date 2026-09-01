# DeepSeek V4 Flash API compatibility

Checked against DeepSeek-owned sources on 2026-09-01 UTC.

## Conclusion

DeepSeek officially offers **DeepSeek-V4-Flash**. The verified API `model`
identifier is **`deepseek-v4-flash`**. DeepSeek currently maps that rolling
identifier to **DeepSeek-V4-Flash-0731** and instructs callers to keep using the
rolling identifier for later updates.

It is safe to change this repository's `DEEPSEEK_MODEL` to
`deepseek-v4-flash` for its existing OpenAI-compatible Chat Completions request.
The base URL and endpoint remain `https://api.deepseek.com/chat/completions`, and
the model supports `response_format: {"type":"json_object"}`.

This is also a required migration: DeepSeek announced that the legacy
`deepseek-chat` and `deepseek-reasoner` identifiers would be discontinued on
2026-07-24. The repository should not continue to rely on `deepseek-chat`.

## Repository compatibility

The current enricher already satisfies two JSON Output requirements:

- it sends `response_format: {"type":"json_object"}`;
- its system prompt explicitly asks for JSON and includes the expected shape.

Two caveats remain:

1. V4 Flash enables thinking mode by default with `high` reasoning effort. A
   model-only configuration change will therefore work, but will not preserve
   the old non-thinking behavior. For short news title and summary generation,
   explicitly sending `"thinking": {"type":"disabled"}` would avoid unnecessary
   reasoning latency and tokens. In thinking mode, the request's current
   `temperature` value is accepted but ignored.
2. DeepSeek recommends setting `max_tokens` to reduce the chance of truncated
   JSON and documents that JSON mode can occasionally return empty content. The
   current code validates malformed or empty results, but does not retry them or
   inspect `finish_reason="length"`.

Because `deepseek-v4-flash` is a rolling alias, DeepSeek may update the served
model without changing the identifier. Output behavior should be monitored
after such updates even though the API contract remains stable.

## Official sources

- [Your First API Call](https://api-docs.deepseek.com/) — current model IDs,
  version mapping, base URL, and Chat Completions example.
- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing) — V4
  Flash model version, context limits, and JSON Output support.
- [Lists Models](https://api-docs.deepseek.com/api/list-models) — official model
  identifier example.
- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
  — `POST /chat/completions`, accepted model IDs, and `response_format` contract.
- [JSON Output](https://api-docs.deepseek.com/guides/json_mode) — prompt,
  `max_tokens`, empty-content, and truncation guidance.
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) — default
  thinking behavior and ignored sampling parameters.
- [DeepSeek V4 Preview Release](https://api-docs.deepseek.com/news/news260424) —
  V4 Chat Completions compatibility and legacy model-name retirement.
- [Change Log](https://api-docs.deepseek.com/updates) — V4 Flash public-beta
  release and rolling model-name policy.

## Addendum: medium thinking request (2026-09-01)

For OpenAI-format Chat Completions, DeepSeek documents the thinking toggle as
`thinking.type` and the effort control as the top-level `reasoning_effort`.
The exact request fragment for a caller that asks for `medium` is:

```json
{
  "model": "deepseek-v4-flash",
  "thinking": {"type": "enabled"},
  "reasoning_effort": "medium",
  "max_tokens": 16384,
  "response_format": {"type": "json_object"},
  "stream": false
}
```

`medium` is accepted only as a compatibility value: DeepSeek maps it to the
actual `high` effort. The native documented effort values are `low`, `high`,
and `max`; `xhigh` is also accepted for compatibility and maps to `high`.
Consequently there is no distinct medium-effort execution tier. Thinking mode
ignores `temperature`, `top_p`, `presence_penalty`, and `frequency_penalty`
without returning an error, so `temperature` should be omitted from this
request rather than relied upon.

DeepSeek lists the maximum V4 Flash output as **384K tokens**. The
`max_tokens: 16384` value above is a repository-specific recommendation, not a
DeepSeek-prescribed default. It is conservative for the current maximum batch:
the parser permits at most 20 items, each with an 80-character title and a
600-character summary, or 13,600 generated content characters before JSON and
IDs. DeepSeek estimates approximately 0.6 token per Chinese character, putting
that content near 8,160 tokens; 16,384 leaves substantial room for JSON syntax,
IDs, and thinking output while still bounding a malformed or runaway response.
The caller should still treat `finish_reason="length"` as a failed batch and
adjust the limit using observed API usage if valid outputs approach the cap.

Additional official sources for this addendum:

- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) — exact
  OpenAI-format fields, effort mapping, and ignored sampling parameters.
- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
  — accepted request fields and JSON truncation warning.
- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing) — 384K
  maximum output for V4 Flash.
- [Token & Token Usage](https://api-docs.deepseek.com/quick_start/token_usage) —
  official approximate Chinese-character/token conversion and its caveat.
