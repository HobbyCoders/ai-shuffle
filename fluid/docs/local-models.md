# Running fluid on local models

The `local` provider kind talks to any OpenAI-compatible `chat/completions`
server — llama.cpp's `llama-server`, Ollama, LM Studio, vLLM, tabbyAPI, and
friends. No account, no API key required.

## Quick start: llama.cpp

```sh
llama-server -m qwen2.5-7b-instruct-q4_k_m.gguf --port 8080
```

The built-in defaults already include a `local` provider pointed at
`http://127.0.0.1:8080`, so this is enough:

```sh
fluid chat --provider local
```

`llama-server` serves whichever model it was started with and ignores the
`model` field in the request, so you don't need to set one.

## Quick start: Ollama

Ollama speaks the OpenAI protocol at `/v1/chat/completions`. Unlike
llama-server it **routes by model name**, so you must set `model` to an
installed model:

```toml
# .fluid/config.toml
[providers.local]
kind = "local"
base_url = "http://127.0.0.1:11434"
model = "qwen2.5:7b"
```

## base_url normalization

`base_url` accepts any of these forms and resolves to the same endpoint:

| You write | Requests go to |
|---|---|
| `http://127.0.0.1:8080` | `http://127.0.0.1:8080/v1/chat/completions` |
| `http://127.0.0.1:8080/v1` | `http://127.0.0.1:8080/v1/chat/completions` |
| `http://host/v1/chat/completions` | used verbatim |
| `http://host/llama` (proxy path) | `http://host/llama/v1/chat/completions` |

Trailing slashes are ignored.

## API keys

An `Authorization: Bearer` header is always sent (placeholder value `local` by
default), which satisfies servers started with `--api-key`. To send a real key:

```toml
[providers.local]
kind = "local"
base_url = "http://127.0.0.1:8080"
api_key_env = "LLAMA_API_KEY"
```

## Text-protocol tool calling

Many local models — small quants especially — don't emit native OpenAI
`tool_calls`, or emit them unreliably. Opt in to the text protocol and tool
calling works anyway:

```toml
[providers.local]
kind = "local"
base_url = "http://127.0.0.1:8080"
tool_style = "text"
```

With `tool_style = "text"`, tool schemas are *not* sent as `tools` in the
request body. Instead:

- a compact protocol block plus each tool's JSON schema is appended to the
  system prompt;
- the model calls tools by emitting
  `<tool_call>{"name": "Read", "arguments": {"path": "a.txt"}}</tool_call>`
  in its reply (several blocks for several calls);
- tool outputs are returned to the model as
  `<tool_result name="Read">…</tool_result>` text;
- the streamed reply is parsed incrementally — marker text is suppressed from
  what you see, and the engine receives the same tool-use events native calling
  would produce. `<think>…</think>` passes through as thinking output.

Malformed JSON inside a `<tool_call>` block still dispatches: the tool layer
returns a clean in-context error the model can react to and retry, instead of
the turn dying.

The default `tool_style` is `"native"` — leave it there for models with solid
native tool calling (llama-server `--jinja` with a capable model, larger
instruction-tuned models).

## Server-specific knobs via `extra`

The request body is plain OpenAI chat/completions. Anything else your server
understands goes through `extra`, which merges into the body last (and can
override any default field):

```toml
[providers.local]
kind = "local"
base_url = "http://127.0.0.1:8080"

[providers.local.extra]
cache_prompt = true     # llama-server: reuse the KV cache across turns
temperature = 0.7
```

Usage accounting degrades gracefully — servers that don't report token counts
show zeros, and there is never a cost figure for local inference.
