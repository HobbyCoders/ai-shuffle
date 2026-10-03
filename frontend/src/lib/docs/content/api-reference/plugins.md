<!--
title: Plugins
description: Plugin marketplaces, installing, enabling and updating plugins, automatic updates, file-based agents, connector plugin runtimes and service sign-in
order: 11
-->

# Plugins API

Endpoints for the workspace-wide plugin library: git marketplaces, the plugins they offer, installing, enabling and updating plugins, and the official connector plugins. Plugins installed into a single project use the [Project Plugins](/docs/api-reference/projects#project-plugins) endpoints instead.

Reads need any valid credential, including an API key (`aih_...`). Every endpoint that changes something needs owner access; API keys get `403`.

### Unreadable settings files

Plugin changes write `settings.json` (which plugins are enabled), `plugins/installed_plugins.json` and `plugins/known_marketplaces.json` in the Fluid home (`~/.fluid`, or `/home/appuser/.fluid` in Docker); project plugin changes write the project's `.fluid/settings.json` or `.fluid/settings.local.json`. When a file the change would write exists but cannot be read as a JSON object (damaged, not UTF-8, not an object, or permission denied), install, uninstall, enable, disable, update and the marketplace add, sync and remove routes return `409` and change nothing: the check runs before any file is deleted, copied or cloned, so the same request works once the file is fixed. For install, update and uninstall this check comes before the marketplace and plugin are looked up, so while such a file is damaged an unknown plugin ID answers `409` rather than `400`. `detail` names the file. When its bytes could be read, a copy is saved next to it as `<name>.<UTC time>.bak` and `detail` names the copy; a copy never overwrites an earlier one or a `.bak` of your own, identical content is saved once, and the five newest copies of each file are kept. Reads still work and show the file as empty. The automatic update settings file is the exception: [PUT /api/v1/plugins/auto-update/settings](#put-apiv1pluginsauto-updatesettings) copies a damaged one the same way and replaces it.

## Plugin IDs

A plugin ID is `name@marketplace`, for example `code-review@my-marketplace`. The official connector plugins use the `shuffle-connectors` marketplace, for example `telegram@shuffle-connectors`. In the per-plugin paths below, `{plugin_id}` is the full ID and may contain `/`; URL-encode it (`@` becomes `%40`), as the web app does.

## Marketplaces

A marketplace is a git repository that lists plugins. Adding one clones it on the host.

### GET /api/v1/plugins/marketplaces

List registered marketplaces.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/plugins/marketplaces \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "my-marketplace",
    "source": "git",
    "url": "https://github.com/acme/my-marketplace.git",
    "install_location": "/home/user/.fluid/plugins/marketplaces/my-marketplace",
    "last_updated": "2025-01-15T10:30:00Z"
  }
]
```

### POST /api/v1/plugins/marketplaces

Register a marketplace and clone it (shallow clone, 120-second limit). Returns `201` with the marketplace.

**Auth:** Owner only

JSON body:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | `https://` or `http://` git URL, `git@host:path` SSH URL, or GitHub shorthand `owner/repo`. GitHub URLs get `.git` appended if missing |
| `name` | string | No | Marketplace ID. Letters, digits, `-` and `_` only. Defaults to the repository name |

```bash
curl -X POST http://localhost:8000/api/v1/plugins/marketplaces \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "acme/my-marketplace"}'
```

Returns `400` for an invalid URL or name and `500` if the clone fails. If a marketplace folder with that name already exists on the host, it is registered without cloning again.

### DELETE /api/v1/plugins/marketplaces/{marketplace_id}

Remove a marketplace, its local copy, and every plugin installed from it, including their cached files. Returns `204`, or `404` if the marketplace is not registered.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/plugins/marketplaces/my-marketplace \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### POST /api/v1/plugins/marketplaces/{marketplace_id}/sync

Pull the latest version of a marketplace from its remote. Installed plugins do not change until you [update](#updates) them.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/plugins/marketplaces/my-marketplace/sync \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{ "status": "synced", "marketplace_id": "my-marketplace" }
```

Returns `404` if the marketplace is not registered and `500` if the git operation fails.

## Plugin discovery

### GET /api/v1/plugins/available

List every plugin offered by the registered marketplaces, plus the official connector plugins (unless you filter to another marketplace), whether installed or not.

**Auth:** Required

| Query | Type | Description |
|-------|------|-------------|
| `marketplace` | string | Only list plugins from this marketplace ID |

```bash
curl "http://localhost:8000/api/v1/plugins/available?marketplace=my-marketplace" \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "code-review@my-marketplace",
    "name": "code-review",
    "marketplace": "my-marketplace",
    "marketplace_path": "/home/user/.fluid/plugins/marketplaces/my-marketplace/plugins/code-review",
    "description": "Review pull requests",
    "has_commands": true,
    "has_agents": true,
    "has_skills": false,
    "has_hooks": false,
    "has_mcp": false,
    "connector_type": null,
    "installed": false,
    "enabled": false
  }
]
```

`connector_type` is set only for connector plugins (for example `telegram`). A marketplace with no readable `marketplace.json` does not break the list: if the repository itself has skills, commands or agents, it is listed as one plugin, `<marketplace>@<marketplace>`; otherwise it is left out. An entry without a usable name (for example one containing `/`) is skipped.

### GET /api/v1/plugins/{plugin_id}

Details for one plugin. Installed plugins are looked up first, then the marketplaces, so this also works for a plugin you have not installed yet.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/plugins/code-review%40my-marketplace \
  -H "Authorization: Bearer aih_your_key"
```

Returns the [installed plugin fields](#get-apiv1pluginsinstalled) plus `commands`, `agents` and `skills` (lists of names) and `readme` (Markdown text or `null`). Returns `404` if no installed or available plugin has that ID.

## Installing and removing plugins

### GET /api/v1/plugins/installed

List installed plugins, including installed connector plugins.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/plugins/installed \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "code-review@my-marketplace",
    "name": "code-review",
    "marketplace": "my-marketplace",
    "description": "Review pull requests",
    "version": "1.2.0",
    "scope": "user",
    "install_path": "/home/user/.fluid/plugins/cache/my-marketplace/code-review/1.2.0",
    "installed_at": "2025-01-15T10:30:00Z",
    "enabled": true,
    "has_commands": true,
    "has_agents": true,
    "has_skills": false,
    "has_hooks": false,
    "has_mcp": false,
    "connector_type": null
  }
]
```

### POST /api/v1/plugins/install

Install one plugin from a marketplace. A marketplace plugin is enabled after install, and installing one again replaces it with the marketplace's current version. Returns `201` with the installed plugin (same fields as the installed list).

**Auth:** Owner only

JSON body:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plugin_id` | string | Yes | `name@marketplace` from the available list |
| `scope` | string | No | Default `user`. Connector plugins accept only `user`. To install into one project, use [Project Plugins](/docs/api-reference/projects#project-plugins) |

```bash
curl -X POST http://localhost:8000/api/v1/plugins/install \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plugin_id": "code-review@my-marketplace"}'
```

Returns `400` when the ID is malformed, the marketplace or plugin is not found, the plugin's source is missing or of an unsupported type, a name or version is not a single plain folder name, a source URL does not start with `https://`, `http://`, `ssh://` or `git@`, or a pinned `sha` is not 7 to 64 hex characters. Returns `500` when the git clone fails, when the pinned `sha` cannot be fetched, and for other install failures; `detail` carries the reason. See [Installing Plugins](/docs/features/plugins#install-a-plugin) for the source formats and the rules on symlinks and paths. Installing a connector plugin sets up its own Python environment, so the host needs access to PyPI and the request can take a while.

### POST /api/v1/plugins/install/batch

Install several plugins in order with one request. Returns `201` with the list of installed plugins.

**Auth:** Owner only

JSON body: `plugin_ids` (array of plugin IDs, required) and `scope` (default `user`).

```bash
curl -X POST http://localhost:8000/api/v1/plugins/install/batch \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plugin_ids": ["code-review@my-marketplace", "docs-writer@my-marketplace"]}'
```

The batch stops at the first plugin that fails and returns that plugin's error, with the same status as a single install (`400`, `409` or `500`); plugins installed before it stay installed. A `409` for an unreadable settings file leaves the failing plugin not installed and changes nothing for it, but plugins earlier in the same batch (for example a connector plugin, which does not use those files) stay installed.

### DELETE /api/v1/plugins/{plugin_id}

Uninstall a plugin and delete its installed files (only files inside the plugin cache are ever deleted). Returns `204`.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/plugins/code-review%40my-marketplace \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

Returns `404` if the plugin is not installed. A connector plugin that saved connections still use cannot be uninstalled: the request also returns `404`, and `detail` names those connections. Remove the connections first.

### POST /api/v1/plugins/{plugin_id}/enable

Enable an installed plugin for the whole workspace.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/plugins/code-review%40my-marketplace/enable \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{ "status": "enabled", "plugin_id": "code-review@my-marketplace" }
```

For a connector plugin, returns `409` if it is not installed or its files fail the integrity check.

### POST /api/v1/plugins/{plugin_id}/disable

Disable a plugin for the whole workspace without uninstalling it. Returns `{"status": "disabled", "plugin_id": "..."}`.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/plugins/code-review%40my-marketplace/disable \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

For a connector plugin, returns `409` if it is not installed or saved connections still use it.

## Updates

### GET /api/v1/plugins/updates

Compare installed marketplace plugins with their marketplaces and list the ones with a newer version. Sync the marketplace first to see its latest plugins.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/plugins/updates \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "plugin_id": "code-review@my-marketplace",
    "name": "code-review",
    "marketplace": "my-marketplace",
    "installed_version": "1.2.0",
    "available_version": "1.3.0",
    "is_local": true
  }
]
```

`is_local` is `true` when the plugin's files live inside the marketplace repository and `false` when the marketplace points to another source.

### POST /api/v1/plugins/update

Update one installed plugin to the marketplace's current version. Its scope and enabled state are kept. For a connector plugin, this installs the release shipped with this AI Shuffle version.

**Auth:** Owner only

JSON body: `plugin_id` (string, required).

```bash
curl -X POST http://localhost:8000/api/v1/plugins/update \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plugin_id": "code-review@my-marketplace"}'
```

Returns the updated plugin (same fields as the installed list), or `400` if the plugin is not installed or cannot be installed.

### POST /api/v1/plugins/update/all

Update every plugin that [GET /api/v1/plugins/updates](#get-apiv1pluginsupdates) lists. Plugins that fail to update are skipped. Returns the list of updated plugins.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/plugins/update/all \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Automatic updates

### GET /api/v1/plugins/auto-update/settings

Read the automatic update settings.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/plugins/auto-update/settings \
  -H "Authorization: Bearer aih_your_key"
```

**Response (defaults):**
```json
{
  "enabled": false,
  "interval_hours": 24,
  "auto_sync_marketplaces": true,
  "auto_update_plugins": true,
  "last_check": null
}
```

`last_check` is the ISO 8601 time of the last update run, or `null`.

### PUT /api/v1/plugins/auto-update/settings

Change the automatic update settings. Send only the fields to change. Returns the full settings.

**Auth:** Owner only

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Run updates on a schedule |
| `interval_hours` | integer | Hours between runs, 1 to 168 |
| `auto_sync_marketplaces` | boolean | Pull every marketplace at the start of a run |
| `auto_update_plugins` | boolean | Install available updates during a run (otherwise a run only checks) |

```bash
curl -X PUT http://localhost:8000/api/v1/plugins/auto-update/settings \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "interval_hours": 12}'
```

An `interval_hours` outside 1 to 168 returns `422`.

### POST /api/v1/plugins/auto-update/run

Run one update pass now, whether or not scheduled updates are enabled. It follows `auto_sync_marketplaces` and `auto_update_plugins`, then sets `last_check`.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/plugins/auto-update/run \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "synced_marketplaces": { "my-marketplace": true },
  "updates_available": [
    {
      "plugin_id": "code-review@my-marketplace",
      "name": "code-review",
      "marketplace": "my-marketplace",
      "installed_version": "1.2.0",
      "available_version": "1.3.0",
      "is_local": true
    }
  ],
  "updated_plugins": [],
  "errors": []
}
```

`synced_marketplaces` maps each marketplace ID to whether its sync succeeded (empty when syncing is off). `updated_plugins` lists the plugins that were updated, and `errors` holds a message for each check or update that failed.

## File-based agents

### GET /api/v1/plugins/agents/file-based

List the agents defined as Markdown files in the `agents/` folder of installed, enabled plugins.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/plugins/agents/file-based \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "code-review@my-marketplace:reviewer",
    "name": "reviewer",
    "description": "Reviews a diff for bugs",
    "model": "sonnet",
    "tools": ["Read", "Grep"],
    "prompt": "You review code changes...",
    "source_plugin": "code-review@my-marketplace",
    "file_path": "/home/user/.fluid/plugins/cache/my-marketplace/code-review/1.2.0/agents/reviewer.md"
  }
]
```

`id` is `<plugin_id>:<file name>`. `description`, `model` and `tools` come from the file's YAML header (`model` and `tools` may be `null`), and `prompt` is the Markdown body.

## Connector runtimes

Official connector plugins (`<type>@shuffle-connectors`) provide the runtime for a connector type. Install, enable, disable, update and uninstall them with the endpoints above; these two endpoints are specific to them. For any `plugin_id` outside the `shuffle-connectors` marketplace they return `404`.

### GET /api/v1/plugins/runtime/{plugin_id}/connections

List the saved connections of this connector type, active or not, with the automations that use each one. Check this before disabling, uninstalling or rolling back a connector plugin.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/plugins/runtime/telegram%40shuffle-connectors/connections \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "id": "conn-1a2b3c4d",
    "name": "Team Telegram",
    "active": true,
    "automations": [
      { "id": "auto_1", "name": "Daily digest", "is_enabled": true, "uses": ["output", "trigger"] }
    ]
  }
]
```

`uses` lists how the automation uses the connection: `trigger`, `action` and/or `output`.

### POST /api/v1/plugins/runtime/{plugin_id}/rollback

Switch a connector plugin back to the version that was installed before the current one. Connection IDs and settings are kept. Returns the plugin (same fields as the installed list).

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/plugins/runtime/telegram%40shuffle-connectors/rollback \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

Returns `409` if there is no previous version, if any connection of this type is active or has an enabled automation (disable the connections and pause the automations first), or if the previous version fails its integrity check.

## Service sign-in (OAuth)

A plugin can declare an outside service it needs and how the owner signs in to it: `extensions["ai.shuffle"].services[].auth` with OAuth 2.0 authorization code and PKCE, public endpoints and scope names only (see [Plugin format](/docs/features/plugins)). The owner supplies the OAuth client and signs in through these endpoints. The client and the tokens are stored encrypted on this worker and are never copied to other devices; each worker signs in on its own. No endpoint returns a client secret, an authorization code or a token.

Only a plugin that is switched on and approved can start a sign-in. An agent cannot sign in, enter a client or read a token; it asks with `RequestChatAccess(kind="service", plugin, service)` and receives a grant handle (see [Private Access](/docs/api-reference/sessions#private-access)).

**Return address.** The sign-in returns to `<address>/api/v1/plugins/oauth/callback`, where `<address>` is the address your browser uses to reach this worker, the same way the Gmail connector forms its redirect URI. Register that exact URL as a redirect URI in the provider's OAuth app. It must be `https://` or `http://localhost` (or `127.0.0.1`); a plain `http://` LAN address is refused. Each address you sign in from (for example the desktop app on `http://localhost:8000` and the worker's HTTPS remote access address) is a separate redirect URI, and the sign-in fails at the provider for an address you did not register. `GET /status` shows the address for the current connection in `redirect_uri`.

**Auth:** Owner only. `PUT /client` additionally needs the desktop app window, the worker's HTTPS address or a browser on the worker's own machine, as for private access inputs; other connections get `403`.

### GET /api/v1/plugins/oauth/status

Query: `plugin` (the plugin ID) and optionally `service`. Without `service`, lists every service the plugin declares a sign-in for.

```json
{
  "plugin_id": "dns-reader@my-marketplace",
  "enabled": true,
  "redirect_uri": "https://shuffle.example.com/api/v1/plugins/oauth/callback",
  "services": [
    {
      "plugin_id": "dns-reader@my-marketplace",
      "service": "cloudflare",
      "state": "signed_in",
      "reason": "",
      "client_configured": true,
      "client_id": "abc123",
      "requested_scopes": ["zone:read"],
      "scopes": ["zone:read"],
      "missing_scopes": [],
      "expires_at": 1790000000.0,
      "can_refresh": true,
      "access": "read",
      "authorize_host": "dash.cloudflare.com"
    }
  ]
}
```

`enabled` is true when the plugin is switched on and approved. `state` is `not_configured` (no OAuth client yet), `signed_out`, `signed_in`, `needs_sign_in` or `invalid` (the plugin's declaration fails validation and cannot be used). For `needs_sign_in`, `reason` is `scopes_widened`, `endpoint_changed`, `client_changed`, `expired` or `unreadable`. `access` is the declared access, `read` (GET and HEAD) or `write`. Returns `404` for a plugin that is not installed or a service it does not declare a sign-in for.

### PUT /api/v1/plugins/oauth/client

Save the OAuth client for a plugin service: `{"plugin": "...", "service": "...", "client_id": "...", "client_secret": "..."}`. `client_secret` is optional (leave it out for a public client). Saving a different `client_id` removes the existing sign-in. Returns the service entry from `GET /status`. Validation errors return `422` and never echo the secret; a malformed client returns `400`; locked credential storage returns `409`.

### DELETE /api/v1/plugins/oauth/client

Query: `plugin` and `service`. Removes the client and its sign-in, also for a plugin that has been uninstalled. Returns `{"deleted": true}` when a client was saved.

### POST /api/v1/plugins/oauth/start

Body: `{"plugin": "...", "service": "..."}`. Returns `authorization_url` (open it in the same browser), `flow_id`, `redirect_uri` and `expires_in` (600 seconds). Sets an HttpOnly cookie scoped to the callback path that ties the sign-in to this browser. The pending sign-in is also tied to the owner session that started it, can be used once, and expires after 10 minutes. Returns `409` when the plugin is off or not approved, or when no client is saved, `400` for a return address that is not HTTPS or localhost, and `429` when 64 sign-ins are already waiting.

### GET /api/v1/plugins/oauth/callback

The provider sends the browser here. Not called directly. It needs the `state` from a pending sign-in, the cookie set by `/start` and the same owner session; an unknown, used or expired `state`, another browser or another session gets `400`. The plugin must still be switched on, approved and declare the same sign-in, or nothing is saved. On success the worker exchanges the code with the provider and stores the tokens. The response is a small page (`Cache-Control: no-store`, no referrer) that posts `{"type": "plugin-oauth", "flow_id", "plugin_id", "service", "state", "error"}` to the window that opened it and closes. It never carries the code or a token.

### POST /api/v1/plugins/oauth/sign-out

Body: `{"plugin": "...", "service": "..."}`. Removes the saved tokens, then revokes them at the provider when the plugin declares a `revoke_url`. Returns `{"signed_out": true, "revoked": true}`; `revoked` is false when there is no revocation endpoint or the provider could not be reached. For a plugin that is no longer installed, the local tokens are still removed.
