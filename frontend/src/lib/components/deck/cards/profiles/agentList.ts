/**
 * Pure helpers behind the Agents page list and detail: which agents a search
 * and the filters show, how the list splits into sections, and how an agent's
 * model reads ("Default" or the pinned model). No Svelte, so they unit-test
 * without rendering.
 */
import type { Profile } from '$lib/api/client';
import { NATIVE_PROVIDER_KEY, VOICE_PROVIDER_KEY } from '$lib/constants/models';

export type AgentFilter = 'all' | 'yours' | 'builtin';
/** Folder filter value for agents in no folder. */
export const FOLDER_NONE = '__none';

export interface ListQuery {
	query: string;
	filter: AgentFilter;
	/** '' = all folders, FOLDER_NONE = no folder, else a folder name. */
	folder: string;
	/** Folder of each agent (groups store assignments). */
	assignments: Record<string, string>;
	/** Folders that still exist; an assignment to a deleted one counts as none. */
	folders: string[];
	/** The open agent stays listed even when the search no longer matches it. */
	selectedId: string | null;
	/** The personal assistant's agent id. */
	assistantId: string | null;
}

export interface AgentSections {
	assistant: Profile[];
	yours: Profile[];
	builtin: Profile[];
	/** Agents that match the query and filters (the kept-open one aside). */
	matches: number;
}

export function folderOf(id: string, assignments: Record<string, string>, folders: string[]): string {
	const f = assignments[id];
	return f && folders.includes(f) ? f : '';
}

/** Search covers name, description, id and folder, as on the old page. */
export function matchesSearch(p: Profile, q: string, folder: string): boolean {
	if (!q) return true;
	return [p.name, p.description, p.id, folder].filter(Boolean).join(' ').toLowerCase().includes(q);
}

export function sectionAgents(profiles: Profile[], opts: ListQuery): AgentSections {
	const q = opts.query.trim().toLowerCase();
	const out: AgentSections = { assistant: [], yours: [], builtin: [], matches: 0 };
	for (const p of profiles) {
		const folder = folderOf(p.id, opts.assignments, opts.folders);
		if (opts.filter === 'yours' && p.is_builtin) continue;
		if (opts.filter === 'builtin' && !p.is_builtin) continue;
		if (opts.folder === FOLDER_NONE && folder) continue;
		if (opts.folder && opts.folder !== FOLDER_NONE && folder !== opts.folder) continue;
		const hit = matchesSearch(p, q, folder);
		if (!hit && p.id !== opts.selectedId) continue;
		if (hit) out.matches += 1;
		if (p.id === opts.assistantId) out.assistant.push(p);
		else if (p.is_builtin) out.builtin.push(p);
		else out.yours.push(p);
	}
	return out;
}

/** Provider rows with a model, the voice leg aside. */
export function pinnedProviders(models: Record<string, string> | null | undefined): string[] {
	return Object.entries(models ?? {})
		.filter(([key, value]) => key && key !== VOICE_PROVIDER_KEY && typeof value === 'string' && value.trim())
		.map(([key]) => key);
}

/** An agent is pinned when it carries a model for at least one provider. */
export function isPinned(models: Record<string, string> | null | undefined): boolean {
	return pinnedProviders(models).length > 0;
}

/** The pinned model to show on a list row: native Claude first, then any provider. */
export function pinnedModel(models: Record<string, string> | null | undefined): string | null {
	const m = models ?? {};
	const native = m[NATIVE_PROVIDER_KEY]?.trim();
	if (native) return native;
	const key = pinnedProviders(m)[0];
	return key ? m[key].trim() : null;
}

/** Profile config's subagent model map, folding in a legacy top-level model. */
export function profileModels(p: Profile): Record<string, string> {
	const config = (p.config ?? {}) as { provider_models?: Record<string, string>; model?: string; provider?: string | null };
	const models = { ...(config.provider_models ?? {}) };
	const key = config.provider || NATIVE_PROVIDER_KEY;
	if (config.model && !models[key]) models[key] = config.model;
	return models;
}

/** A free id for a new agent named `name` ("Release notes" → "release-notes", then "-2"). */
export function uniqueAgentId(name: string, taken: Iterable<string>): string {
	const used = new Set(taken);
	const base =
		name
			.toLowerCase()
			.replace(/[^a-z0-9-]+/g, '-')
			.replace(/-+/g, '-')
			.replace(/^-|-$/g, '') || 'agent';
	let id = base;
	for (let n = 2; used.has(id); n += 1) id = `${base}-${n}`;
	return id;
}

/** "1 tool", "3 tools". */
export function plural(n: number, word: string): string {
	return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
}
