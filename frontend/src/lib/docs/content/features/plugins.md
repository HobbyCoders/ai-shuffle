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

## Updates

**Settings → Plugins** shows plugins with a newer version. Update them one by one or all at once. You can also turn on automatic updates and pick how often they run. Automatic updates never install an older version.

## Plugins made in a chat

Ask an agent for a plugin, for example "make me a plugin that reads my Cloudflare DNS records". It can look through your plugin sources for one that fits, or write a new one.

- Nothing an agent installs is ever turned on by the agent. It waits in **Settings → Plugins**, marked **Needs review**.
- Open it, check each skill, command, agent and MCP server it adds, then choose **Approve and enable**.
- If the plugin changes later, it stops loading until you review it again.
- Plugins an agent writes cannot contain hooks or saved secrets.

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
