/**
 * MCP servers: the data and form state behind Settings > MCP servers.
 *
 * - Servers come from the shared `mcp` store, so the Projects card and this
 *   page always agree.
 * - Each server keeps its own form while the page is open, so switching
 *   between servers never drops unsaved edits. Saving is explicit.
 * - "Add server" makes one local draft (never two).
 * - When the list reloads (a change from another tab or device), a clean form
 *   follows the new values; a dirty form keeps the user's edits and is
 *   flagged "changed elsewhere" or "deleted elsewhere".
 * - Built-in servers are read-only and have no form.
 */
import { Globe, Server, Terminal, Zap } from 'lucide-svelte';
import { mcp } from '$lib/stores/mcp.svelte';
import {
	getServerTypeLabel,
	validateMcpServerConfig,
	type CreateMcpServerRequest,
	type McpServerInfo,
	type McpServerType
} from '$lib/api/mcp';
import { isForbidden } from '$lib/settings/access.svelte';
import type { SettingsIcon } from '$lib/settings/categories';

export const DRAFT_ID = 'new-mcp-server';

export interface Pair {
	key: string;
	value: string;
}

export interface McpForm {
	name: string;
	description: string;
	type: McpServerType;
	command: string;
	/** Space-separated, as typed. */
	args: string;
	url: string;
	env: Pair[];
	headers: Pair[];
}

export type RemoteChange = 'changed' | 'deleted';

export const TYPE_OPTIONS: { value: McpServerType; label: string }[] = [
	{ value: 'stdio', label: 'Local process' },
	{ value: 'http', label: 'HTTP' },
	{ value: 'sse', label: 'SSE' }
];

export function emptyForm(): McpForm {
	return { name: '', description: '', type: 'stdio', command: '', args: '', url: '', env: [], headers: [] };
}

function pairs(record: Record<string, string> | undefined): Pair[] {
	return Object.entries(record ?? {}).map(([key, value]) => ({ key, value }));
}

export function formFromServer(s: McpServerInfo): McpForm {
	return {
		name: s.name,
		description: s.description ?? '',
		type: s.type,
		command: s.command ?? '',
		args: s.args?.join(' ') ?? '',
		url: s.url ?? '',
		env: pairs(s.env),
		headers: pairs(s.headers)
	};
}

function record(list: Pair[]): Record<string, string> | undefined {
	const kept = list.filter((p) => p.key.trim());
	return kept.length ? Object.fromEntries(kept.map((p) => [p.key.trim(), p.value])) : undefined;
}

/** The request body for a form. Fields for the other transport are left out. */
export function configFromForm(f: McpForm): CreateMcpServerRequest {
	const local = f.type === 'stdio';
	const args = f.args.trim();
	return {
		name: f.name.trim(),
		type: f.type,
		command: local ? f.command.trim() : undefined,
		args: local && args ? args.split(/\s+/) : undefined,
		url: local ? undefined : f.url.trim(),
		env: record(f.env),
		headers: local ? undefined : record(f.headers),
		description: f.description.trim() || undefined
	};
}

export function typeIcon(type: string): SettingsIcon {
	switch (type) {
		case 'stdio':
			return Terminal;
		case 'sse':
			return Zap;
		case 'http':
			return Globe;
		default:
			return Server;
	}
}

/** One line under the name: the command, the URL, or what a built-in is. */
export function detailLine(s: McpServerInfo): string {
	if (s.type === 'stdio' && s.command) return s.command + (s.args?.length ? ' ' + s.args.join(' ') : '');
	if ((s.type === 'sse' || s.type === 'http') && s.url) return s.url;
	if (s.cli_flag) return `CLI flag --${s.cli_flag}`;
	if (s.builtin) return 'In-process SDK server';
	return getServerTypeLabel(s.type);
}

function snapshot(f: McpForm): string {
	return JSON.stringify(f);
}

function message(e: unknown, fallback: string): string {
	if (e && typeof e === 'object') {
		const detail = (e as { detail?: unknown; message?: unknown }).detail ?? (e as { message?: unknown }).message;
		if (typeof detail === 'string' && detail) return detail;
	}
	return fallback;
}

export class McpModel {
	loading = $state(false);
	loaded = $state(false);
	forbidden = $state(false);
	loadError = $state('');
	/** Forms by server name (or DRAFT_ID). */
	forms = $state<Record<string, McpForm>>({});
	/** Server values each form started from. */
	private baselines: Record<string, string> = {};
	errors = $state<Record<string, string[]>>({});
	remote = $state<Record<string, RemoteChange>>({});
	saving = $state<string | null>(null);
	draft = $state<McpForm | null>(null);

	get servers(): McpServerInfo[] {
		return mcp.current.globalServers ?? [];
	}

	get custom(): McpServerInfo[] {
		return this.servers.filter((s) => !s.builtin);
	}

	get builtins(): McpServerInfo[] {
		return this.servers.filter((s) => s.builtin);
	}

	server(name: string): McpServerInfo | undefined {
		return this.servers.find((s) => s.name === name);
	}

	async load(): Promise<void> {
		this.loading = true;
		try {
			await mcp.loadGlobalServers();
			this.forbidden = false;
			this.loadError = '';
			this.loaded = true;
			this.reconcile();
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.loadError = 'Could not load MCP servers';
		} finally {
			this.loading = false;
		}
	}

	/** The form for a server or the draft. Call outside rendering (component init). */
	ensure(id: string): McpForm | undefined {
		if (id === DRAFT_ID) return this.draft ?? undefined;
		if (this.forms[id]) return this.forms[id];
		const s = this.server(id);
		if (!s || s.builtin) return undefined;
		this.forms[id] = formFromServer(s);
		this.baselines[id] = snapshot(this.forms[id]);
		return this.forms[id];
	}

	form(id: string): McpForm | undefined {
		return id === DRAFT_ID ? (this.draft ?? undefined) : this.forms[id];
	}

	dirty(id: string): boolean {
		if (id === DRAFT_ID) return this.draft !== null;
		const f = this.forms[id];
		return !!f && snapshot(f) !== this.baselines[id];
	}

	addDraft(): string {
		if (!this.draft) this.draft = emptyForm();
		return DRAFT_ID;
	}

	discardDraft(): void {
		this.draft = null;
		delete this.errors[DRAFT_ID];
	}

	/** Back to the server's current values. */
	discard(id: string): void {
		const s = this.server(id);
		delete this.errors[id];
		delete this.remote[id];
		if (!s) {
			delete this.forms[id];
			delete this.baselines[id];
			return;
		}
		this.forms[id] = formFromServer(s);
		this.baselines[id] = snapshot(this.forms[id]);
	}

	/** Saves the form. Returns the server name on success. */
	async save(id: string): Promise<string | null> {
		const f = this.form(id);
		if (!f || this.saving) return null;
		const config = configFromForm(f);
		const check = validateMcpServerConfig(config);
		if (!check.valid) {
			this.errors[id] = check.errors;
			return null;
		}
		delete this.errors[id];
		this.saving = id;
		try {
			if (id === DRAFT_ID) {
				if (this.server(config.name)) {
					this.errors[id] = [`A server named ${config.name} already exists`];
					return null;
				}
				const created = await mcp.addGlobalServer(config);
				this.draft = null;
				const name = created?.name ?? config.name;
				this.forms[name] = formFromServer(created ?? { ...config, scope: 'user', enabled: true } as McpServerInfo);
				this.baselines[name] = snapshot(this.forms[name]);
				return name;
			}
			const { name: _name, ...update } = config;
			// Deleted elsewhere while the user kept editing: Save adds it back.
			const saved = this.server(id)
				? await mcp.updateGlobalServer(id, update)
				: await mcp.addGlobalServer({ ...update, name: id });
			const fresh = saved ?? this.server(id);
			if (fresh) this.forms[id] = formFromServer(fresh);
			this.baselines[id] = snapshot(this.forms[id]);
			delete this.remote[id];
			return id;
		} catch (e) {
			this.errors[id] = [isForbidden(e) ? 'You need owner access to change servers' : message(e, 'Could not save the server')];
			return null;
		} finally {
			this.saving = null;
			mcp.clearError();
		}
	}

	async remove(id: string): Promise<boolean> {
		try {
			await mcp.removeGlobalServer(id);
			delete this.forms[id];
			delete this.baselines[id];
			delete this.errors[id];
			delete this.remote[id];
			return true;
		} catch (e) {
			this.errors[id] = [message(e, 'Could not delete the server')];
			return false;
		} finally {
			mcp.clearError();
		}
	}

	/** Load the latest server values over the user's edits. */
	loadLatest(id: string): void {
		this.discard(id);
	}

	/** After a reload: clean forms follow the server; dirty forms get flagged. */
	private reconcile(): void {
		for (const id of Object.keys(this.forms)) {
			const s = this.server(id);
			const isDirty = this.dirty(id);
			if (!isDirty) {
				if (s) {
					this.forms[id] = formFromServer(s);
					this.baselines[id] = snapshot(this.forms[id]);
				} else {
					delete this.forms[id];
					delete this.baselines[id];
				}
				delete this.remote[id];
				continue;
			}
			if (!s) this.remote[id] = 'deleted';
			else if (snapshot(formFromServer(s)) !== this.baselines[id]) this.remote[id] = 'changed';
		}
	}
}
