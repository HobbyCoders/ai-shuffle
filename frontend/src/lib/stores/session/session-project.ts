/**
 * Resolve compose project + projectV2Id.
 *
 * Empty string is a real remote-only project, not "unset". Only `undefined`
 * means "not provided" and may fall through to ephemeral or a default.
 * A v2 id with no legacy id must never pick the last local project.
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
}): SessionProjectPick {
	const hasLegacy = input.projectId !== undefined;
	const hasV2 = input.projectV2Id !== undefined;

	const project = hasLegacy ? (input.projectId ?? '') : input.ephemeralProject;
	const projectV2Id = hasV2
		? (input.projectV2Id || null)
		: (input.ephemeralProjectV2Id ?? null);

	if (projectV2Id && !project) {
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

/** New Chat: restore a v2-only compose from ephemeral; otherwise last local default. */
export function resolveNewChatProject(
	ephemeral: { project: string; projectV2Id: string | null } | null | undefined,
	defaultProject: string
): SessionProjectPick {
	if (ephemeral?.projectV2Id && !ephemeral.project) {
		return { project: '', projectV2Id: ephemeral.projectV2Id };
	}
	return { project: defaultProject, projectV2Id: null };
}
