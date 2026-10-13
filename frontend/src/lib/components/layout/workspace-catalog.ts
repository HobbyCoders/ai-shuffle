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
		catalogByLocalId.get(project.id)?.origin ||
		project.link_group_id ||
		`local:${project.id}`;

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
