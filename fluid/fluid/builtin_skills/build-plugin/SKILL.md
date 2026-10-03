---
name: build-plugin
description: Build an AI Shuffle plugin from scratch (skills, slash commands, subagents, MCP servers and outside-service declarations in one reviewed package). Use when the owner wants a reusable capability in every chat and no existing plugin fits.
---

# Build a plugin

Work through the steps in order. The owner switches the plugin on; you never do.

## 1. Plugin or plain skill?

Write a plain skill instead (`save_skill`, or `.fluid/skills/<name>/SKILL.md`
in a project) when one written procedure is enough. Build a plugin when the
owner wants one of these:

- several parts that belong together: skills, slash commands, subagents;
- an MCP server that runs real tools;
- access to an outside service, such as a hosting or DNS API;
- one versioned package that they review and enable as a unit.

Do not build a plugin for a one-off task or to hold a password, key or token.
If the owner has not ruled out an existing plugin, ask before you start.

## 2. Make the folder

Pick a name of lowercase letters, digits and hyphens (`zone-report`), start
at version `0.1.0`, and create `~/.fluid/plugin-drafts/<name>/` with only
these files:

```
<name>/
  plugin.json                 required
  skills/<skill>/SKILL.md     one folder per skill
  commands/<command>.md       optional
  agents/<agent>.md           optional
  mcp.json                    optional
  README.md                   optional, not installed
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

Plugins cannot sign in with OAuth. Do not promise it.

## 6. Run the validator and fix what it reports

```
python -m fluid.context.plugin_check ~/.fluid/plugin-drafts/<name>
```

In a code session without a shell, run the same check in Python:
`from fluid.context.plugin_check import check_plugin_folder` and print
`check_plugin_folder(path).render()`.

It prints `OK: <name> <version> is a valid plugin (...)` or a list of
problems. Fix each problem in the files, then run it again. Repeat until it
says OK. Do not save a plugin that has not passed.

## 7. Save it, switched off

Call `save_plugin` with the content you checked: `name`, `version`,
`description`, `skills` (`[{name, description, body}]`), `commands`
(`[{name, description, body}]`), `agents` (`[{name, description, tools,
body}]`), `mcp_servers` (the object inside `mcpServers`) and `services`.
Each `body` is the file's text after its frontmatter.

`save_plugin` is available in code sessions only. If you do not have it, stop
here and tell the owner where the checked folder is.

The plugin is saved as `<name>@authored` and stays off. Never enable it
yourself: no enable API call, no edit to plugin settings files. To change a
saved plugin, raise `version`, check again and save again. Each version needs
a new review.

## 8. Hand over to the owner

Tell the owner:

1. The plugin id (`<name>@authored`) and version.
2. What it adds: each skill, command, subagent and MCP server, one line each.
3. Each service it will ask access to, and why.
4. What to do: open Settings > Plugins, review the plugin, and choose
   **Approve and enable**. Then finish the private access card the first time
   a skill asks for a service.

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
