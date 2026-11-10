<!--
title: MCP Servers
description: Give your agents more tools with Model Context Protocol servers
order: 3
-->

# MCP Servers

MCP (Model Context Protocol) servers give your agents more tools: a database, a file system, GitHub, a search service and many more. Add a server, and its tools show up next to the agent's own tools.

Only the owner can add, change or remove servers.

## Add a server

1. Open **Settings → MCP servers** for a server every project can use. For a server only one project uses, open the project's **MCP Servers** tab instead.
2. Choose **Add server**.
3. Give it a name and pick its type:
   - **Command (stdio):** AI Shuffle runs a program on your worker, for example `npx -y @modelcontextprotocol/server-filesystem /workspace/docs`. Add any environment variables it needs, such as an access token.
   - **HTTP:** a server at a URL, with any headers it needs, such as an API key.
   - **SSE:** an older kind of URL server. It only works with Claude models, so use HTTP when the server offers it.
4. Save. Use the switch next to a server to turn it off and on.

Values you enter, such as tokens and keys, are only shown to you as the owner. Anyone else sees `********`.

## Where a server applies

- **Everywhere:** servers added under **Settings → MCP servers**.
- **One project, just for you:** kept with the project but out of Git.
- **One project, shared:** saved in the project's `.mcp.json`, so everyone who clones the repository gets it.
- **Installed with the Claude Code CLI:** servers you added to a project with the CLI also show up, and you can turn them off here.

## Built-in servers

AI Shuffle comes with a built-in browser server, so agents can open and use web pages. You can turn it off, but not remove it.

## Choosing which tools an agent gets

An MCP server's tools are named `mcp__<server>__<tool>`, for example `mcp__github__create_issue`. In an agent's **Tools** section you can give it a whole server or only some of its tools. See [Tool configuration](/docs/features/profiles#tool-configuration).

## Examples

**A folder (command):**

```json
{
  "name": "filesystem",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace/docs"]
}
```

**GitHub (command, with a token):**

```json
{
  "name": "github",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxxxxxxxxxxx" }
}
```

**A remote server (HTTP):**

```json
{
  "name": "api-tools",
  "type": "http",
  "url": "https://mcp.example.com/mcp",
  "headers": { "X-API-Key": "your-api-key" }
}
```

## Optional: use your own test phone or emulator

[mobile-mcp](https://github.com/mobile-next/mobile-mcp) is a third-party MCP server. With it, an agent can see and use an Android or iOS device that your worker can already reach, for example to test your own app. AI Shuffle does not install it and nothing here is on by default.

This is separate from the device tools in the AI Shuffle Android app. Those ask you on the phone before each action. mobile-mcp does not: its tools act on the device directly.

**Add it to one project.** On the project's **MCP Servers** tab, add a server just for you:

```json
{
  "name": "mobile",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@mobilenext/mobile-mcp@1.0.16"],
  "env": { "MOBILEMCP_DISABLE_TELEMETRY": "1" }
}
```

- Keep the version number. Without it, `npx` runs whichever version is newest that day.
- The worker needs Node.js 20 or newer.
- `MOBILEMCP_DISABLE_TELEMETRY` turns off the usage reports the package otherwise sends to its maker.
- Keep the command form shown. The package can also run as a network server (`--listen`), which accepts anyone who can reach it unless you set a token.

**Give the agent only the tools it needs.** The server offers 34 tools. In the agent's **Tools** section, pick tools one by one, not the whole server. These are enough to look at an app and use it:

`mobile_list_available_devices`, `mobile_launch_app`, `mobile_list_elements_on_screen`, `mobile_take_screenshot`, `mobile_click_on_screen_at_coordinates`, `mobile_type_keys`, `mobile_swipe_on_screen`, `mobile_press_button`

Leave these four off: `mobile_login_to_cloud_provider`, `mobile_list_remote_devices`, `mobile_allocate_remote_device` and `mobile_release_remote_device`. They rent devices from the maker's cloud service, which can cost money, and when no local device is found the server suggests that service to the agent.

**What the worker needs.** The server runs on the worker that runs the chat, so that worker must reach the device itself. AI Shuffle does not pass a device from one worker to another.

- Android: the Android platform tools (`adb`) on the worker, and either a phone with USB debugging allowed for that worker or an emulator that is already running.
- iOS simulator: a Mac worker with Xcode and a simulator that is already started.

**Stay safe.** Use a test device or emulator with test accounts, not your everyday phone: an agent with these tools can read whatever is on the screen and can install and remove apps. Do not open `adb` to your network.

**Turn it off.** Use the server's switch, or remove it. The package also starts a small helper program, `mobilecli`, that keeps running for up to 30 minutes after its last use. To stop it at once, run `npx -y mobilecli@1.0.22 daemon stop` on the worker.

**What we have checked.** With version 1.0.16 on a Linux worker with Node.js 22 and no device attached, AI Shuffle starts the server, lists its 34 tools and gets a clear "no device" answer. We have not yet checked it with a real Android phone, an Android emulator, an iOS simulator or an iPhone, so treat those as untested.

To manage MCP servers from your own code, see the [MCP API](/docs/api-reference/mcp).
