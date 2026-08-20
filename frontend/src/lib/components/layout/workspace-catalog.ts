/**
 * Workspace picker grouping for the cluster project catalog.
 *
 * Local `GET /projects` rows stay the default list. Catalog rows with
 * `remote_only` land in the "On other runners" group. Foreign
 * `projects.path` is never displayed — the catalog response does not
 * carry a path.
 */

import type { ClusterProject } from '$lib/api/installs';
import type { Project } from '$lib/stores/types';

export const ON_OTHER_RUNNERS = 'On other runners';

export interface WorkspacePickerItem {
	id: string;
	name: string;
	/** Legacy `projects` id on this install, or '' for remote-only. */
	project: string;
	projectV2Id: string | null;
	remoteOnly: boolean;
}

/**
 * Merge local projects with the cluster catalog.
 *
 * Local rows keep their legacy id (so existing selection / persistence
 * still match). Remote-only catalog rows use `project_v2_id` as the
 * picker id. A catalog row that already has a local id is used only to
 * stamp `projectV2Id` onto the local item — it is not duplicated.
 */
export function buildWorkspacePickerItems(
	localProjects: Pick<Project, 'id' | 'name'>[],
	catalog: ClusterProject[]
): { items: WorkspacePickerItem[]; remoteItems: WorkspacePickerItem[] } {
	const catalogByLocalId = new Map<string, ClusterProject>();
	const remote: WorkspacePickerItem[] = [];
	for (const row of catalog) {
		if (row.local_project_id) {
			catalogByLocalId.set(row.local_project_id, row);
			continue;
		}
		if (!row.remote_only) continue;
		remote.push({
			id: row.project_v2_id,
			name: row.name,
			project: '',
			projectV2Id: row.project_v2_id,
			remoteOnly: true,
		});
	}

	const items: WorkspacePickerItem[] = localProjects.map((p) => {
		const linked = catalogByLocalId.get(p.id);
		return {
			id: p.id,
			name: p.name,
			project: p.id,
			projectV2Id: linked?.project_v2_id ?? null,
			remoteOnly: false,
		};
	});

	return { items, remoteItems: remote };
}

/** Selected picker id: local legacy id wins, else the v2 id. */
export function selectedPickerId(
	project: string | null | undefined,
	projectV2Id: string | null | undefined
): string | null {
	return project || projectV2Id || null;
}

export function pickerItemName(
	items: WorkspacePickerItem[],
	remoteItems: WorkspacePickerItem[],
	selectedId: string | null
): string | null {
	if (!selectedId) return null;
	return (
		items.find((i) => i.id === selectedId)?.name ??
		remoteItems.find((i) => i.id === selectedId)?.name ??
		null
	);
}
