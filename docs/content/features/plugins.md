<!--
title: Plugins
description: Add skills, commands, agents and tools to AI Shuffle with plugin packages
order: 5
-->

# Plugins

A plugin is a package that adds skills, slash commands, agents, MCP servers or hooks to AI Shuffle. Plugins come from plugin sources (Git repositories, also called marketplaces), or an agent can make one for you.

Manage plugins in **Settings → Plugins**. Only the owner can install, remove, update or turn plugins on and off.

## What a plugin can add

- **Skills** that teach agents how to do something.
- **Slash commands.**
- **Agents** with their own prompts and tools.
- **MCP servers** with extra tools.
- **Hooks** that run at key moments. See [Plugin-contributed hooks](/docs/features/hooks#plugin-contributed-hooks).

AI Shuffle reads Claude Code plugins and Codex (Agent Plugins) plugins as they are. A few parts that only make sense in those tools are adjusted or left out. For example, a plugin agent always runs on the model you chose for the chat. The plugin's details list everything that was adjusted or left out, and why.

## Add a plugin source

1. Open **Settings → Plugins**.
2. Add a source by its Git URL, or GitHub's short form `owner/repo`.
3. Its plugins appear under **Explore**.

**Sync** fetches a source's latest plugins. Removing a source also removes the plugins you installed from it.

## Install a plugin

1. Find the plugin under **Explore**.
2. Choose **Install**. It installs switched off.
3. Check what it adds: its hooks and MCP servers run code on your worker.
4. Turn it on.

Turned-on plugins load in every chat. You can also turn a plugin on for one agent only, in that agent's **Plugins** section. A plugin can also be installed into one project instead of the whole worker.

Their slash commands and user-invocable skills appear in the composer's **/** menu in **Code** mode on both Claude-model and API-model chats. The same applies to your own Fluid command and skill files in `~/.fluid` and the project's `.fluid` or `.agents` directories, within the agent's enabled settings scopes. AI Shuffle expands their prompts and arguments before sending them; it does not run inline shell snippets in those prompts. If Claude Code has a command or skill with the same name, the Claude chat keeps Claude Code's version and execution path. **Chat** mode does not load these files or plugins, so its menu only offers the runtime's built-in commands.

## Updates

**Settings → Plugins → Sources and updates → Check for updates** refreshes the sources of installed plugins, then checks for new releases or changed content. Repositories without a marketplace catalog and plugins whose version string has not changed are included. A same-version update shows a short content identifier after `+`.

Before an external source has been checked, Settings says **Not checked**, not **Up to date**. A failed check is shown by name and keeps the last-known update results; healthy plugins can still be updated. **Up to date** means the tracked plugins were successfully checked. A missing installed folder is offered as an update or repair, even at an unchanged commit pin.

Update plugins one by one or all at once. A manual Update refreshes its source first and keeps the plugin's enabled state. Git sources follow their configured branch or tag; an exact commit pin stays pinned until the source entry changes. Local-directory sources use the files already on disk. A folder imported as an agent-authored draft is a reviewed snapshot, not a tracked upstream: import a new draft to change it.

You can turn on automatic updates and pick how often they run. The `auto_sync_marketplaces` setting controls refreshing marketplace checkouts and checking external git sources; **Install updates** controls applying detected changes. Opening Settings does not contact external plugin repositories: it shows cached checks until the owner checks again or automatic source sync runs. The candidate plugin manifest is canonical, with the catalog version used only as a fallback. Updates never install an older release, including when a commit pin changes. Updating a catalog-free repository preserves its passive skills/commands/agents-only type; adopting newly published hooks or MCP servers requires a fresh owner install. New chats load the applicable user/global installed revision, even when older commit hashes or project records remain in the cache.

**Not checked** means no external check has run for this install; only a successful comparison can say **Up to date**. If a source check fails, Settings names the failing plugin and keeps the last-known result alongside healthy updates. A missing installed cache can be repaired through Check, Update all or opted-in automatic updates, even when its release or commit pin has not changed.

## Plugins made in a chat

Ask an agent for a plugin, for example "make me a plugin that reads my Cloudflare DNS records". It can look through your plugin sources for one that fits, or write a new one.

- Nothing an agent installs is ever turned on by the agent. It waits in **Settings → Plugins**, marked **Needs review**.
- Open it, check each skill, command, agent and MCP server it adds, then choose **Approve and enable**.
- If the plugin changes later, it stops loading until you review it again.
- Plugins an agent writes cannot contain hooks or saved secrets.
- Once approved and enabled, their slash commands appear in both Claude-model and API-model chats. AI Shuffle expands the command's body and arguments before sending it; Claude's own commands still run through Claude Code.

## Plugins that use outside services

A plugin that talks to an outside service, such as Cloudflare, says so. Its details show the service, what it needs access for, and how you sign in.

- **With an API key:** when the agent first needs it, you enter the key in a private access card. The agent never sees the key.
- **With a sign-in:** you sign in to the service from the card in your browser. Some plugins sign in through a short confirmation page on hobbycoders.com. Only choose **Continue** there if you just started that sign-in in AI Shuffle.

If a sign-in through hobbycoders.com is not available, for example because the worker is not connected to your account, the card offers **Set up manually**, where you enter your own sign-in app details.

A plugin update that asks for more access needs a new sign-in.

## Official connector apps

The official connector apps, such as Webhook, Discord Bot and Gmail, come with AI Shuffle. Install them from **Settings → Plugins** with no GitHub account. Installing one needs internet access to download its parts. New versions arrive with AI Shuffle updates.

Before updating a connector app, pause the automations that use it and turn off its connections. Your settings are kept, and you can roll back to the previous version.

To manage plugins from your own code, see the [Plugins API](/docs/api-reference/plugins).
