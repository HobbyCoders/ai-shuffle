/**
 * Pinned generation choices for one scheduled automation job (#507, #543).
 *
 * Owner-only `GET/PUT/DELETE /generation/jobs/{automation_id}/choices[/{kind}]`
 * (app/api/generation_choices.py). A job choice pins an account, its holding
 * worker and a model, plus declared options. It is separate from the
 * account's remembered choice and from catalog favourites: writing one never
 * changes either. With no job choice a run uses the remembered choice, or
 * skips that generation step and reports it. Price, options and caps are
 * rechecked on every run.
 */
import { api } from './client';
import type { GenerationChoice, GenerationKind } from '$lib/utils/generation-choice';

const KIND_ORDER: readonly GenerationKind[] = ['image', 'video', 'audio', '3d'];

export type JobChoices =Partial<Record<GenerationKind, GenerationChoice>>;

/**
 * Edits staged in the automation form until Save: a choice sets or replaces
 * the job's pick for that kind, `null` clears it, and a missing kind is left
 * as saved.
 */
export type JobChoiceDraft = Partial<Record<GenerationKind, GenerationChoice | null>>;

function path(jobId: string, kind?: GenerationKind): string {
	const base = `/generation/jobs/${encodeURIComponent(jobId)}/choices`;
	return kind ? `${base}/${encodeURIComponent(kind)}` : base;
}

/** The body the API accepts: no `origin`, which only remembered picks carry. */
function body(choice: GenerationChoice): Omit<GenerationChoice, 'origin'> {
	const { origin: _drop, ...rest } = choice;
	void _drop;
	return { ...rest, options: rest.options ?? {} };
}

/** Rejects with the server's 503 when a stored job choice cannot be read. */
export async function getJobChoices(jobId: string): Promise<JobChoices> {
	const data = await api.get<{ job_id: string; choices: JobChoices }>(path(jobId));
	return data?.choices ?? {};
}

export async function saveJobChoice(
	jobId: string,
	kind: GenerationKind,
	choice: GenerationChoice
): Promise<GenerationChoice> {
	if (!choice.account || !choice.worker || !choice.model) {
		throw new Error('A job choice must name its account, holding worker and model.');
	}
	const data = await api.put<{ choice: GenerationChoice }>(path(jobId, kind), body(choice));
	return data.choice;
}

export async function clearJobChoice(jobId: string, kind: GenerationKind): Promise<void> {
	await api.delete(path(jobId, kind));
}

export function hasDraftChanges(draft: JobChoiceDraft): boolean {
	return Object.keys(draft).length > 0;
}

export interface DraftCommitResult {
	/** Kinds whose write failed, still staged so a retry sends them again. */
	remaining: JobChoiceDraft;
	/** `kind: message` for every failed write, in kind order. */
	errors: string[];
}

/**
 * Write every staged edit for a job. Each kind is written on its own, so one
 * failure does not undo the others; failed kinds stay in `remaining`.
 */
export async function commitJobChoiceDraft(
	jobId: string,
	draft: JobChoiceDraft,
	describeError: (e: unknown) => string
): Promise<DraftCommitResult> {
	const remaining: JobChoiceDraft = {};
	const errors: string[] = [];
	for (const kind of KIND_ORDER) {
		const value = draft[kind];
		if (value === undefined) continue;
		try {
			if (value === null) await clearJobChoice(jobId, kind);
			else await saveJobChoice(jobId, kind, value);
		} catch (e) {
			remaining[kind] = value;
			errors.push(`${kind}: ${describeError(e)}`);
		}
	}
	return { remaining, errors };
}
