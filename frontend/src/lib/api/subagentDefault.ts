/**
 * The global subagent default (issues #1827, #1830): one model every agent
 * without a pinned model runs on as a subagent.
 *
 *   GET / PUT /api/v1/settings/subagent-default
 *   { model: string | null }
 *
 * A worker without this endpoint answers 404; callers then get `null` and
 * keep the per-provider pickers.
 */
import { api } from './client';

export interface SubagentDefault {
	model: string | null;
}

export const SUBAGENT_DEFAULT_PATH = '/settings/subagent-default';

function isNotFound(err: unknown): boolean {
	return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 404;
}

/** Normalise whatever the server sent into the contract shape. */
export function normalizeSubagentDefault(raw: unknown): SubagentDefault {
	const value = (raw ?? {}) as Partial<SubagentDefault>;
	return {
		model: typeof value.model === 'string' && value.model.trim() ? value.model : null
	};
}

/** The current default, or null when this worker has no such endpoint (404). */
export async function getSubagentDefault(): Promise<SubagentDefault | null> {
	try {
		return normalizeSubagentDefault(await api.get<unknown>(SUBAGENT_DEFAULT_PATH));
	} catch (err) {
		if (isNotFound(err)) return null;
		throw err;
	}
}

/** Save the default model. */
export async function putSubagentDefault(next: { model: string | null }): Promise<SubagentDefault> {
	const saved = await api.put<unknown>(SUBAGENT_DEFAULT_PATH, { model: next.model });
	return normalizeSubagentDefault(saved);
}
