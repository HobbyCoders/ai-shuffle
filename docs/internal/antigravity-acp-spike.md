# Antigravity over ACP: spike results and design

AI Shuffle can use Google's own Antigravity agent (`agy acp`) as a delegated
leg. This is the Google counterpart of the Claude CLI leg: the app starts
Google's unmodified binary as a subprocess and drives it over the Agent Client
Protocol (ACP v1, JSON-RPC over stdio). This page records what the binary
actually advertised when probed, and how the leg is built.

Terms rule: use only Google's unmodified binary. Never read, copy, store, sync
or refresh its sign-in files or keyring entry, and never call Google's backend
with them. `tests/core/test_antigravity_no_token_access.py` fails if app or
fluid code names those files.

## Spike (agy 1.5.6, linux-x64, inside the worker container)

Install: the official manifest
(`…/manifests/linux_amd64.json` → `{version, url, sha512}`) points at a
tarball holding one file, `antigravity`. Google's install script puts it at
`~/.local/bin/agy`. The binary runs in the Docker container without extra
libraries.

`initialize` (client sent protocolVersion 1, no terminal capability) returned:

```json
{
  "protocolVersion": 1,
  "agentCapabilities": {
    "loadSession": true,
    "promptCapabilities": {"image": true, "audio": false, "embeddedContext": true},
    "sessionCapabilities": {"resume": {}, "close": {}, "load": {}, "list": {}, "additionalDirectories": {}},
    "auth": {"logout": {}},
    "_meta": {"antigravity": {"tier": true, "systemPrompt": {"append": true}}}
  },
  "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.6"},
  "authMethods": [
    {"id": "oauth-personal", "name": "Log in with Google"},
    {"id": "oauth-business", "name": "Log in with Gemini Enterprise"},
    {"id": "gemini-api-key", "name": "Gemini API key"},
    {"id": "agent-platform", "name": "Gemini Enterprise Agent Platform"}
  ]
}
```

The reply also duplicates `load_session` and `session_capabilities` in
snake_case, and each auth method has a `description` (both left out above).

- The reply has **no standard `systemPrompt`** in sessionCapabilities. It has
  the vendor extension `_meta.antigravity.systemPrompt.append`.
- The reply has **no `mcpCapabilities`**, so the agent takes only stdio MCP
  servers. HTTP MCP servers would need `--process-per-session`, which the app
  does not use.
- `terminal-login` (type terminal, args `["--login"]`) is listed only when the
  client sends `clientCapabilities.auth.terminal: true`.

Without credentials:

- `session/new` and `session/list` return
  `{"code": -32000, "message": "Authentication required"}`.
- `authenticate` with the method id:
  - `oauth-personal` returns -32603: "No browser is available here (no
    graphical display is available to open a browser). Choose \"Log in from
    the terminal\" instead, or use a Gemini API key."
  - `oauth-business` returns -32603: the same message, ending "Use a Gemini API
    key or Gemini Enterprise Agent Platform instead." Business sign-in has
    **no headless flow**.
  - `agent-platform` returns -32602: it needs GOOGLE_CLOUD_PROJECT and
    GOOGLE_CLOUD_LOCATION plus Application Default Credentials.
  - `gemini-api-key` with no key returns -32602: "GEMINI_API_KEY is not set;
    pass an API key in _meta.apiKey or set the GEMINI_API_KEY environment
    variable." With a key in the environment it returns `{}` and does not check
    the key.

Headless personal sign-in: `agy acp --login` prints

```
Open this link in a browser to log in with Google:

  https://accounts.google.com/o/oauth2/auth?…&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback…

Then paste the authorization code, or the full URL of the page you land on, and press Enter:
```

and reads the code on stdin. The binary stores the result itself, under its
home directory.

With a deliberately invalid key (not a Google credential), the real binary
went further:

- `session/new` (with `_meta.antigravity.systemPrompt.append`) succeeded. It
  returned:
  - `sessionId`;
  - `models` (legacy ids such as `gemini-3.1-pro-high`);
  - `modes` (`default`, `accept-edits`, `plan`);
  - `configOptions`: `model` (gemini-4-argon, gemini-3.8-flash,
    gemini-3.7-flash, gemini-3.6-flash, gemini-3.1-pro), `thought_level`
    (low/high for 3.1-pro) and `mode`.
- An `available_commands_update` notification followed.
- `session/set_config_option` for model and thought_level was accepted.
- A prompt echoed a `user_message_chunk`, then failed **in band**: an
  `agent_message_chunk` of "⚠ Agent execution terminated due to error." and
  stopReason `end_turn`. It was not an RPC error.
- The stdio MCP servers we passed were launched. The agent sent
  `server/discover`, then `initialize` (MCP 2025-11-25), then `tools/list`.

`/usage`: with no sign-in, `agy -p /usage --output-format json` printed a
sign-in link on stderr and waited 60 s. Stdout then held
`{"conversation_id":"","status":"ERROR","error":"authentication failed or timed out","usage":{…},"finished_reason":"error"}`.
The signed-in shape was not seen.

State directory: `~/.gemini/antigravity-cli` (plus `~/.gemini/config/`). The
app gives every provider row its own `HOME` under
`<data_dir>/antigravity/homes/<row id>`, so rows never share a sign-in and the
app never opens the files inside.

## Design

- **fluid/fluid/engine/acp/**:
  - `client.py`: the generic ACP connection (JSON-RPC, `initialize`,
    capabilities).
  - `mapping.py`: session updates become fluid blocks. Text and thought chunks
    become Text and Thinking. Tool calls and updates become ToolUse and
    ToolResult, with diffs shown. Plans become TodoWrite.
  - `loop.py`: the ExecutionEngine.
  - `antigravity.py`: Google specifics only (auth ids, environment, model and
    thinking-level mapping, the in-band error text).
- **The engine handles:**
  - `session/new`, `load` and `resume`, and `prompt` with images when
    advertised.
  - `Interrupt` sends `session/cancel`. `Steer` during a turn sends a second
    `session/prompt`.
  - `session/request_permission` goes to the app's permission prompt and
    permission modes. `elicitation/create` goes to the user-question flow.
  - Model and effort go through `session/set_config_option`; effort maps to
    `thought_level`.
  - After an app restart, a Resume re-sends a prompt that got no answer.
  - Shutdown closes the subprocess cleanly.
  - Usage is recorded only when ACP reports it.
- **App side:**
  - `app/core/antigravity.py` holds install, status, sign-in through the
    binary's own `--login`, the guard, and `/usage`.
  - `app/api/antigravity.py` is the admin API.
  - `harness_adapter._connect_antigravity` connects the leg. Its
    `_provider_row_kind` returns `antigravity`.
  - Rows use provider_type `antigravity`. `auth_mode` holds the ACP auth
    method id (`oauth-personal`, `oauth-business` or `gemini-api-key`).
  - A Gemini key is stored encrypted like other provider keys. It is handed
    only to the subprocess environment as `GEMINI_API_KEY`.
- **Capability gate:** network flag `antigravity_acp_v1`. Row creation answers
  409 while a peer worker lacks it, and 503 if the check fails.
- **Guard:** rows on personal sign-in run only in chats the owner types into.
  They are refused for automations, scheduled or automatic turns,
  Task/Swarm/subagent children, agent-started sessions and background calls.
  The per-row override `AI_SHUFFLE_ANTIGRAVITY_ALLOW_UNATTENDED` (off by
  default) lives in the row's extra env and is never passed to the agent.
- **Quota:** usage source `antigravity_usage` runs `agy -p /usage
  --output-format json`, only when the usage popover asks (30 s forced-refresh
  cooldown). It parses defensively; anything else shows "unavailable" with the
  dashboard link.

### What reaches the agent

- **Instructions:** the profile prompt plus the delegated append pieces arrive
  as `_meta.antigravity.systemPrompt.append` on `session/new`. Memory and the
  system-reminder context arrive in the prompt text, as for the Claude leg.
- **Tools in:** the app's registry tools reach the agent through the
  `fluid_bundled` stdio MCP bridge (chat-access, app-config, builtins and the
  rest of the delegated surface, after the delegated blocklist). The session's
  external stdio MCP servers are passed as-is.
- **Tools not passed:**
  - HTTP and SSE MCP servers, because the agent advertises no mcpCapabilities;
  - fluid's own file and shell tools, because the agent has its own and its
    permission requests come to the app;
  - all tools for proactive sessions.

## Unverified (needs a real Google sign-in or key)

- Turns after `session/new` against the real binary with valid credentials:
  streamed chunk and tool-call shapes, the title format of bridged tool calls,
  permission option ids, and whether turns report usage.
- Whether a stored sign-in still needs an explicit `authenticate` call.
- The signed-in `/usage` JSON shape.
- Completing a real sign-in with a real code.
- Model and thinking-level option ids for accounts with other tiers.
