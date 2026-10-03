/**
 * Hooks: the data and form state behind Settings > Hooks.
 *
 * - Hooks come from the shared `hooks` store. With a project picked, the
 *   store loads that project's hooks plus app hooks; without one, app hooks.
 *   Plugin hooks come along and are read-only.
 * - Each hook keeps its own form while the page is open; saving is explicit.
 *   Enabled is the exception: it is a switch that saves at once.
 * - Scope (app or a project) is chosen when a hook is created and fixed after.
 * - When the list reloads, a clean form follows the new values; a dirty form
 *   keeps its edits and is flagged "changed elsewhere" or "deleted elsewhere".
 */
import { Globe, Terminal } from 'lucide-svelte';
import { hooks } from '$lib/stores/hooks.svelte';
import type { Hook, HookCreate, HookEventType, HookTestResult, HookType, HookUpdate, PluginHookInfo } from '$lib/api/hooks';
import { isForbidden } from '$lib/settings/access.svelte';
import type { SettingsIcon } from '$lib/settings/categories';

export const DRAFT_ID = 'new-hook';
export const DEFAULT_TIMEOUT_MS = 30_000;

export const EVENT_HINTS: Record<HookEventType, string> = {
	PreToolUse: 'Before a tool runs',
	PostToolUse: 'After a tool finishes',
	Stop: 'When a reply ends',
	SessionStart: 'When a session starts',
	SessionEnd: 'When a session closes',
	UserPromptSubmit: 'When you send a prompt',
	PreCompact: 'Before history compaction',
	SubagentStop: 'When a subagent finishes',
	Notification: 'When an agent notifies'
};

/** Order in the event picker: tool events, reply, session, then the rest. */
export const EVENT_ORDER: HookEventType[] = [
	'PreToolUse',
	'PostToolUse',
	'Stop',
	'SubagentStop',
	'SessionStart',
	'SessionEnd',
	'UserPromptSubmit',
	'Notification',
	'PreCompact'
];

export function isToolEvent(event: string): boolean {
	return event === 'PreToolUse' || event === 'PostToolUse';
}

export interface HookForm {
	name: string;
	/** null = app hook. Fixed once the hook exists. */
	projectId: string | null;
	event: HookEventType;
	type: HookType;
	command: string;
	/** Seconds in the form; the API stores milliseconds. */
	timeoutSec: number;
	url: string;
	secret: string;
	matcher: string;
	/** Only used by the draft; existing hooks toggle at once. */
	enabled: boolean;
}

export type RemoteChange = 'changed' | 'deleted';

export interface PluginHookRow extends PluginHookInfo {
	id: string;
}

export function emptyForm(projectId: string | null): HookForm {
	return {
		name: '',
		projectId,
		event: 'Stop',
		type: 'bash',
		command: '',
		timeoutSec: DEFAULT_TIMEOUT_MS / 1000,
		url: '',
		secret: '',
		matcher: '',
		enabled: true
	};
}

export function formFromHook(h: Hook): HookForm {
	return {
		name: h.name,
		projectId: h.project_id ?? null,
		event: h.event,
		type: h.hook_type,
		command: h.command ?? '',
		timeoutSec: Math.round((h.timeout || DEFAULT_TIMEOUT_MS) / 1000),
		url: h.url ?? '',
		secret: h.secret ?? '',
		matcher: h.tool_matcher ?? '',
		enabled: h.enabled
	};
}

/** What Save would change; `enabled` is left out (it saves on its own). */
function editable(f: HookForm): string {
	const { enabled: _enabled, ...rest } = f;
	return JSON.stringify(rest);
}

export function validateForm(f: HookForm): string[] {
	const errors: string[] = [];
	if (!f.name.trim()) errors.push('Name is required');
	if (f.type === 'bash' && !f.command.trim()) errors.push('Command is required');
	if (f.type === 'webhook' && !f.url.trim()) errors.push('URL is required');
	return errors;
}

const clampTimeout = (sec: number) => Math.min(300, Math.max(1, Math.round(sec || 30))) * 1000;

export function createPayload(f: HookForm): HookCreate {
	const bash = f.type === 'bash';
	return {
		name: f.name.trim(),
		event: f.event,
		hook_type: f.type,
		command: bash ? f.command.trim() : undefined,
		timeout: clampTimeout(f.timeoutSec),
		url: bash ? undefined : f.url.trim(),
		secret: !bash && f.secret.trim() ? f.secret.trim() : undefined,
		tool_matcher: isToolEvent(f.event) && f.matcher.trim() ? f.matcher.trim() : undefined,
		enabled: f.enabled
	};
}

/**
 * The PATCH body. The worker skips null fields, so a cleared matcher or
 * secret is sent as an empty string (an empty matcher matches every tool).
 */
export function updatePayload(f: HookForm): HookUpdate {
	const bash = f.type === 'bash';
	return {
		name: f.name.trim(),
		event: f.event,
		hook_type: f.type,
		command: bash ? f.command.trim() : undefined,
		timeout: clampTimeout(f.timeoutSec),
		url: bash ? undefined : f.url.trim(),
		secret: bash ? undefined : f.secret.trim(),
		tool_matcher: isToolEvent(f.event) ? f.matcher.trim() : ''
	};
}

export function hookIcon(type: string): SettingsIcon {
	return type === 'webhook' ? Globe : Terminal;
}

/** "PreToolUse, Bash" style summary. */
export function hookSummary(h: { event: string; tool_matcher?: string | null }): string {
	return isToolEvent(h.event) && h.tool_matcher ? `${h.event}, ${h.tool_matcher}` : h.event;
}

function message(e: unknown, fallback: string): string {
	if (e && typeof e === 'object') {
		const detail = (e as { detail?: unknown; message?: unknown }).detail ?? (e as { message?: unknown }).message;
		if (typeof detail === 'string' && detail) return detail;
	}
	return fallback;
}

export class HooksModel {
	/** The project whose hooks are listed; null lists app hooks only. */
	projectId = $state<string | null>(null);
	loading = $state(false);
	loaded = $state(false);
	forbidden = $state(false);
	loadError = $state('');
	forms = $state<Record<string, HookForm>>({});
	private baselines: Record<string, string> = {};
	errors = $state<Record<string, string[]>>({});
	remote = $state<Record<string, RemoteChange>>({});
	saving = $state<string | null>(null);
	testing = $state<string | null>(null);
	results = $state<Record<string, HookTestResult>>({});
	draft = $state<HookForm | null>(null);

	get all(): Hook[] {
		return hooks.current.hooks ?? [];
	}

	get app(): Hook[] {
		return this.all.filter((h) => !h.project_id);
	}

	get project(): Hook[] {
		return this.projectId ? this.all.filter((h) => h.project_id === this.projectId) : [];
	}

	get plugin(): PluginHookRow[] {
		const seen = new Map<string, number>();
		return (hooks.current.pluginHooks ?? []).map((p) => {
			const base = `plugin:${p.plugin_id}:${p.event}`;
			const n = seen.get(base) ?? 0;
			seen.set(base, n + 1);
			return { ...p, id: n ? `${base}:${n}` : base };
		});
	}

	hook(id: string): Hook | undefined {
		return this.all.find((h) => h.id === id);
	}

	pluginHook(id: string): PluginHookRow | undefined {
		return this.plugin.find((p) => p.id === id);
	}

	async load(): Promise<void> {
		this.loading = true;
		try {
			if (this.projectId) await hooks.loadAll(this.projectId);
			else await hooks.loadAppHooks();
			this.forbidden = false;
			this.loadError = '';
			this.loaded = true;
			this.reconcile();
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.loadError = 'Could not load hooks';
		} finally {
			this.loading = false;
			hooks.clearError();
		}
	}

	setProject(projectId: string | null): void {
		if (projectId === this.projectId) return;
		this.projectId = projectId;
		void this.load();
	}

	/** The form for a hook or the draft. Call outside rendering (component init). */
	ensure(id: string): HookForm | undefined {
		if (id === DRAFT_ID) return this.draft ?? undefined;
		if (this.forms[id]) return this.forms[id];
		const h = this.hook(id);
		if (!h) return undefined;
		this.forms[id] = formFromHook(h);
		this.baselines[id] = editable(this.forms[id]);
		return this.forms[id];
	}

	form(id: string): HookForm | undefined {
		return id === DRAFT_ID ? (this.draft ?? undefined) : this.forms[id];
	}

	dirty(id: string): boolean {
		if (id === DRAFT_ID) return this.draft !== null;
		const f = this.forms[id];
		return !!f && editable(f) !== this.baselines[id];
	}

	addDraft(): string {
		if (!this.draft) this.draft = emptyForm(null);
		return DRAFT_ID;
	}

	discardDraft(): void {
		this.draft = null;
		delete this.errors[DRAFT_ID];
	}

	discard(id: string): void {
		const h = this.hook(id);
		delete this.errors[id];
		delete this.remote[id];
		if (!h) {
			delete this.forms[id];
			delete this.baselines[id];
			return;
		}
		this.forms[id] = formFromHook(h);
		this.baselines[id] = editable(this.forms[id]);
	}

	loadLatest(id: string): void {
		this.discard(id);
	}

	/** Saves the form. Returns the hook id on success. */
	async save(id: string): Promise<string | null> {
		const f = this.form(id);
		if (!f || this.saving) return null;
		const problems = validateForm(f);
		if (problems.length) {
			this.errors[id] = problems;
			return null;
		}
		delete this.errors[id];
		this.saving = id;
		try {
			if (id === DRAFT_ID || !this.hook(id)) {
				// A draft, or a hook deleted elsewhere while it was being edited.
				const body = createPayload(id === DRAFT_ID ? f : { ...f, enabled: this.hook(id)?.enabled ?? f.enabled });
				const created = f.projectId ? await hooks.createHook(f.projectId, body) : await hooks.createAppHook(body);
				if (id === DRAFT_ID) this.draft = null;
				else {
					delete this.forms[id];
					delete this.baselines[id];
					delete this.remote[id];
				}
				this.forms[created.id] = formFromHook(created);
				this.baselines[created.id] = editable(this.forms[created.id]);
				return created.id;
			}
			const saved = await hooks.updateHook(id, updatePayload(f));
			this.forms[id] = formFromHook(saved);
			this.baselines[id] = editable(this.forms[id]);
			delete this.remote[id];
			return id;
		} catch (e) {
			this.errors[id] = [isForbidden(e) ? 'You need owner access to change hooks' : message(e, 'Could not save the hook')];
			return null;
		} finally {
			this.saving = null;
			hooks.clearError();
		}
	}

	/** Enabled saves at once, outside the form. */
	async setEnabled(id: string, enabled: boolean): Promise<boolean> {
		try {
			const saved = await hooks.updateHook(id, { enabled });
			const f = this.forms[id];
			if (f) f.enabled = saved.enabled;
			return true;
		} catch (e) {
			this.errors[id] = [message(e, 'Could not change the hook')];
			return false;
		} finally {
			hooks.clearError();
		}
	}

	async test(id: string): Promise<void> {
		if (this.testing) return;
		this.testing = id;
		delete this.results[id];
		try {
			this.results[id] = await hooks.testHook(id);
		} catch (e) {
			this.results[id] = { success: false, duration_ms: null, output: null, error: message(e, 'Test failed') };
		} finally {
			this.testing = null;
			hooks.clearError();
		}
	}

	dismissResult(id: string): void {
		delete this.results[id];
	}

	async remove(id: string): Promise<boolean> {
		try {
			await hooks.deleteHook(id);
			delete this.forms[id];
			delete this.baselines[id];
			delete this.errors[id];
			delete this.remote[id];
			delete this.results[id];
			return true;
		} catch (e) {
			this.errors[id] = [message(e, 'Could not delete the hook')];
			return false;
		} finally {
			hooks.clearError();
		}
	}

	private reconcile(): void {
		for (const id of Object.keys(this.forms)) {
			const h = this.hook(id);
			if (!this.dirty(id)) {
				if (h) {
					this.forms[id] = formFromHook(h);
					this.baselines[id] = editable(this.forms[id]);
				} else {
					delete this.forms[id];
					delete this.baselines[id];
				}
				delete this.remote[id];
				continue;
			}
			if (h) {
				this.forms[id].enabled = h.enabled;
				if (editable(formFromHook(h)) !== this.baselines[id]) this.remote[id] = 'changed';
			} else if (this.belongsToLoadedScope(id)) {
				this.remote[id] = 'deleted';
			}
		}
	}

	/** A dirty form for another project's hook is not "deleted" just because it isn't listed. */
	private belongsToLoadedScope(id: string): boolean {
		const f = this.forms[id];
		return !!f && (f.projectId === null || f.projectId === this.projectId);
	}
}
