/**
 * Resolve compose project + projectV2Id.
 *
 * Empty string is a real remote-only project, not "unset". Only `undefined`
 * means "not provided" and may fall through to ephemeral or a default.
 * A v2 id with no legacy id must never pick the last local project.
 *
 * Who runs the chat decides whether a legacy id counts (#1659). Legacy ids are
 * per worker: on a chat another worker runs (`isRemote`), the id on this
 * worker's copy of the row, or one cached from it, can name an unrelated
 * project here. The page then holds the shared id alone, exactly as after a
 * project is attached to such a chat (`adoptAttachedProject`). Without a
 * shared id there is nothing else to hold, so the legacy id stays. A chat this
 * worker runs keeps its legacy id whatever its shared id is: a relinked or
 * unlinked project's chats keep their first shared id by design.
 */

export interface SessionProjectPick {
	project: string;
	projectV2Id: string | null;
}

export function resolveSessionProject(input: {
	projectId?: string | null;
	projectV2Id?: string | null;
	ephemeralProject?: string;
	ephemeralProjectV2Id?: string | null;
	/** Another worker runs the chat. `undefined` is "not known yet": the legacy id stays. */
	isRemote?: boolean;
}): SessionProjectPick {
	const hasLegacy = input.projectId !== undefined;
	const hasV2 = input.projectV2Id !== undefined;

	const project = hasLegacy ? (input.projectId ?? '') : input.ephemeralProject;
	const projectV2Id = hasV2
		? (input.projectV2Id || null)
		: (input.ephemeralProjectV2Id ?? null);

	if (projectV2Id && (input.isRemote || !project)) {
		return { project: '', projectV2Id };
	}
	if (project !== undefined) {
		return { project, projectV2Id };
	}
	return { project: '', projectV2Id: null };
}

/**
 * The project on THIS worker whose agents the composer offers. A chat this
 * worker runs names it directly. A chat another worker runs holds only its
 * shared id (above), so its agents come from this worker's own checkout of
 * that same project, found through the catalog by the shared id; the legacy
 * id, which may name an unrelated project here, is never used. No local
 * checkout: no project agents.
 */
export function agentProjectOnThisWorker(
	state: { project: string; projectV2Id: string | null },
	projects: readonly { id: string }[],
	catalog: readonly { project_v2_id: string; local_project_id: string | null }[]
): string | undefined {
	if (state.project) return state.project;
	if (!state.projectV2Id) return undefined;
	const local = catalog.find((p) => p.project_v2_id === state.projectV2Id)?.local_project_id;
	return local && projects.some((p) => p.id === local) ? local : undefined;
}

/** Persisted snapshot / skip-restore: v2-only keeps `''`, never the last local default. */
export function resolvePersistedProject(
	persisted: { project?: string; projectV2Id?: string | null },
	defaultProject: string
): SessionProjectPick {
	const projectV2Id = persisted.projectV2Id ?? null;
	const project = persisted.project ?? '';
	if (projectV2Id && !project) {
		return { project: '', projectV2Id };
	}
	return { project: project || defaultProject, projectV2Id };
}
