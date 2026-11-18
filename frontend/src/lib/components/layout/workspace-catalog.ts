/**
 * Workspace picker merge for the cluster project catalog.
 *
 * Local `GET /projects` rows and projects available on other online installs
 * share one flat list. The catalog is origin-keyed, so clones of the same Git
 * repository collapse to one picker item. Foreign `projects.path` is never
 * displayed — the catalog response does not carry a path.
 */

import type { ClusterProject } from '$lib/api/installs';
import type { Project } from '$lib/stores/types';
import { projectIdentity } from './session-card-data';

export interface WorkspacePickerItem {
	id: string;
	name: string;
	/** Legacy `projects` id on this install, or '' for remote-only. */
	project: string;
	/** Other local ids collapsed into this same-origin item. */
	projectAliases?: string[];
	projectV2Id: string | null;
	remoteOnly: boolean;
}

type LocalProject = Pick<Project, 'id' | 'name'> & {
	link_group_id?: string | null;
};

/**
 * Merge all locally selectable projects with the online cluster catalog.
 *
 * Local rows keep a legacy id (so existing selection / persistence still
 * match). Projects sharing a `link_group_id` / catalog origin are represented
 * once, preferring the catalog's local id when it is present. Remote-only rows
 * use `project_v2_id` as the picker id and are included only when at least one
 * approved eligible install is online.
 */
export function buildWorkspacePickerItems(
	localProjects: LocalProject[],
	catalog: ClusterProject[]
): WorkspacePickerItem[] {
	const catalogByLocalId = new Map<string, ClusterProject>();
	const catalogByOrigin = new Map<string, ClusterProject>();
	for (const row of catalog) {
		if (row.local_project_id) {
			catalogByLocalId.set(row.local_project_id, row);
		}
		// The backend guarantees one row per origin. Keeping the first also makes
		// the client defensive against a mixed-version server returning duplicates.
		if (!catalogByOrigin.has(row.origin)) catalogByOrigin.set(row.origin, row);
	}

	const localById = new Map(localProjects.map((project) => [project.id, project]));
	const identityFor = (project: LocalProject): string =>
		projectIdentity({
			link_group_id: project.link_group_id,
			origin: catalogByLocalId.get(project.id)?.origin,
			id: project.id
		}) as string;

	const items: WorkspacePickerItem[] = [];
	const includedIdentities = new Set<string>();
	for (const project of localProjects) {
		const identity = identityFor(project);
		if (includedIdentities.has(identity)) continue;

		const linked = catalogByOrigin.get(identity) ?? catalogByLocalId.get(project.id);
		const preferredLocal = linked?.local_project_id
			? localById.get(linked.local_project_id)
			: undefined;
		const representative = preferredLocal ?? project;
		const projectAliases = localProjects
			.filter((candidate) => identityFor(candidate) === identity)
			.map((candidate) => candidate.id)
			.filter((id) => id !== representative.id);
		items.push({
			id: representative.id,
			name: representative.name,
			project: representative.id,
			...(projectAliases.length > 0 ? { projectAliases } : {}),
			projectV2Id: linked?.project_v2_id ?? null,
			remoteOnly: false,
		});
		includedIdentities.add(identity);
	}

	for (const row of catalog) {
		if (!row.eligible.some((install) => install.online)) continue;
		if (includedIdentities.has(row.origin)) continue;
		// A row of a project listed above, under the origin it had before its link.
		if (row.local_project_id && localById.has(row.local_project_id)) continue;
		items.push({
			id: row.project_v2_id,
			name: row.name,
			project: '',
			projectV2Id: row.project_v2_id,
			remoteOnly: true,
		});
		includedIdentities.add(row.origin);
	}

	return items;
}

/** Resolve a persisted local/v2 selection to its merged picker item id. */
export function selectedPickerId(
	project: string | null | undefined,
	projectV2Id: string | null | undefined,
	items: WorkspacePickerItem[] = []
): string | null {
	if (project) {
		return (
			items.find(
				(item) => item.project === project || item.projectAliases?.includes(project)
			)?.id ?? project
		);
	}
	if (projectV2Id) {
		return items.find((item) => item.projectV2Id === projectV2Id)?.id ?? projectV2Id;
	}
	return null;
}

export function pickerItemName(
	items: WorkspacePickerItem[],
	selectedId: string | null
): string | null {
	if (!selectedId) return null;
	return items.find((i) => i.id === selectedId)?.name ?? null;
}

/** The session fields recency ordering reads. */
export interface ProjectRecencySession {
	project_id: string | null;
	project_v2_id?: string | null;
	last_user_message_at?: string | null;
	updated_at?: string | null;
	created_at?: string | null;
}

/**
 * When each picker item was last used: the newest session in that project
 * (its last user message, else its last update), from the session list the
 * sidebar already holds. A session counts for the item with its shared
 * project id first (a project only on another worker has no local id), then
 * its local id or an alias. Items with no session are absent.
 */
export function projectRecency(
	items: WorkspacePickerItem[],
	sessions: ProjectRecencySession[]
): Map<string, number> {
	const itemByShared = new Map<string, string>();
	const itemByProject = new Map<string, string>();
	for (const item of items) {
		if (item.projectV2Id) itemByShared.set(item.projectV2Id, item.id);
		if (item.project) itemByProject.set(item.project, item.id);
		for (const alias of item.projectAliases ?? []) itemByProject.set(alias, item.id);
	}
	const recency = new Map<string, number>();
	for (const session of sessions) {
		const id =
			(session.project_v2_id ? itemByShared.get(session.project_v2_id) : undefined) ??
			(session.project_id ? itemByProject.get(session.project_id) : undefined);
		if (!id) continue;
		const at = Date.parse(session.last_user_message_at || session.updated_at || session.created_at || '');
		if (!Number.isFinite(at)) continue;
		if (at > (recency.get(id) ?? -Infinity)) recency.set(id, at);
	}
	return recency;
}

/**
 * How well `name` matches `query` (both lowercased); lower is better, null is
 * no match: whole name, prefix, start of a word, anywhere, then the query's
 * letters in order (fuzzy).
 */
function matchRank(name: string, query: string): number | null {
	if (name === query) return 0;
	if (name.startsWith(query)) return 1;
	const at = name.indexOf(query);
	if (at > 0 && /[^a-z0-9]/.test(name[at - 1])) return 2;
	if (at > 0) return 3;
	let from = 0;
	for (const char of query) {
		if (/\s/.test(char)) continue;
		from = name.indexOf(char, from) + 1;
		if (from === 0) return null;
	}
	return 4;
}

/**
 * The quick project picker's rows. With no query: the most recently used
 * projects first, then the rest in catalog order. With a query: only the
 * matching projects, best match first, ties by recency then catalog order.
 */
export function rankPickerItems<T extends { id: string; name: string }>(
	items: T[],
	query: string,
	recency: Map<string, number> = new Map()
): T[] {
	const q = query.trim().toLowerCase();
	const scored: { item: T; rank: number; at: number; index: number }[] = [];
	items.forEach((item, index) => {
		const rank = q ? matchRank(item.name.toLowerCase(), q) : 0;
		if (rank === null) return;
		scored.push({ item, rank, at: recency.get(item.id) ?? -Infinity, index });
	});
	scored.sort((a, b) => a.rank - b.rank || b.at - a.at || a.index - b.index);
	return scored.map((entry) => entry.item);
}
