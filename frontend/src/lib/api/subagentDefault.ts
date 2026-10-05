/**
 * The global subagent default (issue #1827): one model every agent without a
 * pinned model runs on as a subagent, plus the short list the lead agent may
 * pick from for a task.
 *
 *   GET / PUT /api/v1/settings/subagent-default
 *   { model: string | null, allowed: string[] }
 *
 * A worker without this endpoint answers 404; callers then get `null` and
 * keep the per-provider pickers.
 */
import { api } from './client';

export interface SubagentDefault {
	model: string | null;
	allowed: string[];
}

export const SUBAGENT_DEFAULT_PATH = '/settings/subagent-default';

function isNotFound(err: unknown): boolean {
	return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 404;
}

/** Normalise whatever the server sent into the contract shape. */
export function normalizeSubagentDefault(raw: unknown): SubagentDefault {
	const value = (raw ?? {}) as Partial<SubagentDefault>;
	return {
		model: typeof value.model === 'string' && value.model.trim() ? value.model : null,
		allowed: Array.isArray(value.allowed) ? value.allowed.filter((m): m is string => typeof m === 'string' && !!m) : []
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

/** Save the default model and the allowed list. */
export async function putSubagentDefault(next: { model: string | null; allowed: string[] }): Promise<SubagentDefault> {
	const saved = await api.put<unknown>(SUBAGENT_DEFAULT_PATH, { model: next.model, allowed: next.allowed });
	return normalizeSubagentDefault(saved);
}
