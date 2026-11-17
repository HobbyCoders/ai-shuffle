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
