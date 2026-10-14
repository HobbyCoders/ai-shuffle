---
name: build-plugin
description: Find, install or build an AI Shuffle plugin (skills, slash commands, subagents, MCP servers and outside-service declarations in one reviewed package). Use when the owner wants a reusable capability in every chat, or asks for a plugin.
---

# Build a plugin

Work through the steps in order. The owner switches the plugin on; you never do.
You need the tools `FindPlugins`, `CheckPlugin` and `InstallPlugin`. If this
chat does not have them, say so and ask the owner to continue in a code
session.

## 1. Plugin or plain skill?

Write a plain skill instead (`save_skill`, or `.fluid/skills/<name>/SKILL.md`
in a project) when one written procedure is enough. Build a plugin when the
owner wants one of these:

- several parts that belong together: skills, slash commands, subagents;
- an MCP server that runs real tools;
- access to an outside service, such as a hosting or DNS API;
- one versioned package that they review and enable as a unit.

Do not build a plugin for a one-off task or to hold a password, key or token.

Look for an existing plugin first: call `FindPlugins` with two or three
keywords.

- Installed and on: tell the owner it is already there. Stop.
- Installed but off, or waiting for review: tell the owner to turn it on in
  Settings > Plugins. Stop.
- Available: call `CheckPlugin` with its `plugin_id` and tell the owner what it
  would load, what was adapted or left out, what runs code, and which
  services it asks for. If it fits, call `InstallPlugin` with the same
  `plugin_id` and go to step 8.
- Nothing fits: build one (steps 2 to 8). Never add a plugin source yourself;
  if the owner names a repository, ask them to add it in Settings > Plugins.

## 2. Make the folder

Pick a name of lowercase letters, digits and hyphens (`zone-report`) and run:
`python -m fluid.context.plugin_check --new <name>`.
This creates `~/.fluid/plugin-drafts/<name>/` with `plugin.json` at `0.1.0`,
an empty `skills/` folder and `AGENTS.md`. It refuses to overwrite an existing
draft. Read its `AGENTS.md` before editing, including when resuming an older
draft. Replace the manifest's placeholder description and add at least one
component; an empty scaffold is not installable. Only these files are allowed:

```
<name>/
  plugin.json                 required
  skills/<skill>/SKILL.md     one folder per skill
  commands/<command>.md       optional
  agents/<agent>.md           optional
  mcp.json                    optional
  README.md                   optional, not installed
  AGENTS.md                   draft rules, not installed
  CLAUDE.md                   optional draft rules, not installed
```

Nothing else: no hooks, scripts, binaries, `.env` files, or another tool's
manifest folder (`.claude-plugin/`, `.codex-plugin/`).

## 3. Write plugin.json

Required: `name`, `version` (like `1.2.3`) and `description` (one sentence).
Point at each part you have, and leave out the ones you don't:

- `"skills": "./skills/"`
- `"mcpServers": "./mcp.json"`
- under `"extensions": {"ai.shuffle": {...}}`: `"commands": "./commands/"`,
  `"agents": "./agents/"` and `"services": [...]` (step 5).

`author` is also allowed. No other keys.

## 4. Write the parts

**Skill**, `skills/<skill>/SKILL.md`. Frontmatter holds `name` (the folder
name) and `description` (what it does and when to use it). The body is
numbered steps that name the exact tools to call.

**Command**, `commands/<command>.md`. Frontmatter holds an optional
`description`. The body is the prompt to run. The owner types
`/<plugin>:<command>`, and `$ARGUMENTS` in the body becomes what they typed
after it.

**Subagent**, `agents/<agent>.md`. Frontmatter holds `name` (the file name),
`description` (when to delegate to it) and an optional `tools` list. Never
add `model`, `effort`, a provider, `permissionMode` or `hooks`: the model and
effort are chosen per chat and permissions come from the chat. Leave `tools`
out to give the agent the calling chat's tools. When you list tools, use only
names that exist here: Read, Write, Edit, Bash, Grep, Glob, WebFetch,
WebSearch, Task, Monitor, Skill, AskUser, ToolSearch, Note, Remember, Recall,
SessionSearch, SessionView, ListAgents, SendMessage, or an MCP tool written
`mcp__<server>__<tool>`. Claude Code names such as `MultiEdit` or
`AskUserQuestion` are refused.

**MCP servers**, `mcp.json`:

```
{"mcpServers": {
  "<server>": {"command": "npx", "args": ["-y", "<package>"], "env": {"API_BASE": "${API_BASE}"}},
  "<other>": {"url": "https://example.com/mcp"}
}}
```

Every `env` value is a `${VAR}` reference that the owner sets on the worker,
never a literal value.

## 5. Outside services: by name only

No file in the plugin ever holds a key, token or password, and you never ask
the owner to paste one into the chat. Declare each service in
`extensions."ai.shuffle".services` with these keys only:

- `name`: lowercase, hyphenated;
- `provider`: `cloudflare` or `custom`;
- `url`: the public `https://` API address (required for `custom`);
- `access`: `read` or `write`;
- `purpose`: one line the owner reads when approving.

The skill that uses the service then tells the agent to:

1. Call `ChatResources` and look for an approved grant for the service.
2. If there is none, call `RequestChatAccess` with `kind` `service`, the
   provider, the API address and the purpose. Tell the owner to finish the
   private access card, and stop until they say it is ready.
3. Call `ChatHttpRequest` with that grant. The worker attaches the credential;
   no model sees it.

A service can also declare an OAuth sign-in under `auth` (see the Plugin
format page): `type` `oauth2`, `authorize_url`, `token_url`, `scopes`,
`"pkce": true`, and optionally `revoke_url`. Add `client_id` only for a
public client already registered with the provider for this plugin (the
owner gives you its ID), and `registration_url` only when the service
documents dynamic client registration. Never a client secret. Then the agent asks with
`RequestChatAccess` naming the plugin and service, and the owner signs in.

## 6. Check it and fix what it reports

Call `CheckPlugin` with `path` set to the folder. It reports
`OK: <name> <version> is a valid plugin` with what will load and the services
it asks for, or a list of problems. Fix each problem in the files, then check
again. Repeat until it says OK. Do not install a plugin that has not passed.

The same check runs from a shell:
`python -m fluid.context.plugin_check ~/.fluid/plugin-drafts/<name>`.

## 7. Install it, switched off

Call `InstallPlugin` with `path` set to the checked folder. It runs the same
check again and installs the plugin as `<name>@authored`, switched off.

Never enable it yourself: no enable API call, no edit to plugin settings
files. To change an installed plugin, raise `version`, check again and install
again. Each version needs a new review.

## 8. Hand over to the owner

Tell the owner:

1. The plugin id (`<name>@authored`, or `name@source` for one from a plugin
   source) and version.
2. What it adds: each skill, command, subagent and MCP server, one line each.
3. Each service it will ask access to, and why.
4. What to do: open Settings > Plugins, where it shows as waiting for review,
   check what it adds, and choose **Approve and enable**. Then finish the
   private access card the first time a skill asks for a service.

## Example

A plugin with one skill, one command and one subagent that reads the owner's
Cloudflare DNS.

**zone-report/plugin.json**

```json
{
  "name": "zone-report",
  "version": "0.1.0",
  "description": "Report and review the DNS records in the owner's Cloudflare zones.",
  "skills": "./skills/",
  "extensions": {
    "ai.shuffle": {
      "commands": "./commands/",
      "agents": "./agents/",
      "services": [
        {"name": "cloudflare", "provider": "cloudflare", "access": "read",
         "purpose": "List zones and their DNS records"}
      ]
    }
  }
}
```

**zone-report/skills/zone-report/SKILL.md**

```markdown
---
name: zone-report
description: List the DNS records in the owner's Cloudflare zones and flag likely mistakes. Use when the owner asks about their DNS.
---

1. Call ChatResources and look for an approved Cloudflare service grant.
2. If there is none, call RequestChatAccess with kind "service", service_provider "cloudflare" and purpose "List zones and their DNS records". Tell the owner to finish the access card, then stop until they say it is ready.
3. Call ChatHttpRequest with the grant, method GET and path /zones.
4. For each zone, call ChatHttpRequest with method GET and path /zones/<zone id>/dns_records.
5. Give the record list to the record-auditor subagent.
6. Report one table per zone (type, name, content, proxied), then the auditor's findings.
```

**zone-report/commands/dns.md**

```markdown
---
description: Report the DNS records for one zone, or every zone
---

Use the zone-report skill. Zone: $ARGUMENTS (every zone when empty).
```

**zone-report/agents/record-auditor.md**

```markdown
---
name: record-auditor
description: Review a list of DNS records and flag likely mistakes. Use after fetching records.
tools: Read
---

You receive DNS records as text. Flag duplicates, records that point at private addresses, CNAMEs at a zone apex, and unproxied web records. Return one line per finding with the record name and the reason.
```
