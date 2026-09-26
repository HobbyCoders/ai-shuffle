/**
 * Installs (cluster) API client.
 *
 * On a shared database every AI Shuffle install self-registers into the
 * `installs` registry. These endpoints power the Devices panel and the Cloud
 * accordion's runner picker — listing installs, renaming/removing a device,
 * and inspecting or rotating the shared cluster secret. Phones live in
 * `mobile_devices` and are not returned here.
 */

import { api } from './client';

export interface Install {
	id: string; // instance_id for legacy installs; device_id for network workers
	device_id?: string; // present on network directory rows; managed through the Devices workers list
	legacy_instance_id?: string | null;
	device_name: string;
	base_url: string | null; // null = view-only (no peer control / offload target)
	platform: string | null;
	version: string | null;
	capabilities: Record<string, unknown>;
	last_seen: string | null;
	online: boolean; // derived server-side from last_seen
	approved?: boolean; // absent on an older server means approved (backward compatible)
	approved_at?: string | null;
	revoked_at?: string | null;
	is_self?: boolean; // annotated by GET /cluster/installs
	created_at?: string;
	updated_at?: string;
}

export interface InstallsResponse {
	installs: Install[];
	self_instance_id: string;
}

/** List every registered install (annotated with `is_self`). */
export async function listInstalls(): Promise<InstallsResponse> {
	return api.get<InstallsResponse>('/cluster/installs');
}

/** Rename a device (the friendly name shown on its sessions). */
export async function renameInstall(id: string, device_name: string): Promise<Install> {
	return api.patch<Install>(`/cluster/installs/${id}`, { device_name });
}

/** Remove a stale install from the registry (it re-registers if it comes back). */
export async function deleteInstall(id: string): Promise<void> {
	await api.delete(`/cluster/installs/${id}`);
}

/** Approve a quarantined runner or revoke an existing one. */
export async function setInstallApproval(id: string, approved: boolean): Promise<Install & { restart_required?: boolean }> {
	return api.put<Install & { restart_required?: boolean }>(`/cluster/installs/${id}/approval`, { approved });
}

// ─── Cluster git state (W3 / ruling D6) ──────────────────────────────────────
//
// `GET /cluster/git-state?project_id=<legacy project id>` reports, per install,
// whether that runner can run this project (`eligible` — it holds an ACTIVE base
// checkout) plus every checkout row it owns, and the project's recent `git_ops`.
// Consumed by the composer device picker (eligibility badges).

/** One `checkouts` row: the base checkout (`branch` null/'') or a worktree. */
export interface CheckoutRow {
	branch: string | null; // null / '' = the project's base checkout
	path: string;
	head_sha: string | null;
	dirty: boolean;
	status: string | null;
	updated_at: string | null;
}

/** An install row annotated with this project's git state on that runner. */
export interface GitStateInstall extends Install {
	eligible: boolean; // has an ACTIVE base checkout of this project
	availability_error?: string; // checkout state could not be read; not proof of absence
	checkouts: CheckoutRow[];
}

/** A row from the durable `git_ops` queue (ruling D5). */
export interface GitOpRow {
	id: string;
	runner_id: string;
	op: string;
	status: 'queued' | 'running' | 'done' | 'error' | (string & {});
	params: { branch?: string; [key: string]: unknown } | null;
	result: Record<string, unknown> | null;
	created_at: string;
	updated_at: string | null;
}

export interface ClusterGitState {
	project_v2_id: string | null; // null = not a git project (no cluster git state)
	installs: GitStateInstall[];
	recent_ops: GitOpRow[];
}

/**
 * Query string for `GET /cluster/git-state`. Exactly one of `projectId` /
 * `projectV2Id`. Returns null when neither is set (skip the fetch — fail-open).
 */
export function clusterGitStatePath(
	projectId?: string | null,
	projectV2Id?: string | null
): string | null {
	if (projectId) {
		return `/cluster/git-state?project_id=${encodeURIComponent(projectId)}`;
	}
	if (projectV2Id) {
		return `/cluster/git-state?project_v2_id=${encodeURIComponent(projectV2Id)}`;
	}
	return null;
}

/** Per-install git state for a project (eligibility, checkouts, recent ops). */
export async function getClusterGitState(
	projectId?: string | null,
	projectV2Id?: string | null
): Promise<ClusterGitState> {
	const path = clusterGitStatePath(projectId, projectV2Id);
	if (!path) {
		throw { detail: 'pass exactly one of project_id or project_v2_id', status: 400 };
	}
	return api.get<ClusterGitState>(path);
}

/** One `projects_v2` available on at least one approved online install. */
export interface ClusterProjectEligible {
	install_id: string;
	device_name: string;
	online: boolean;
	is_self: boolean;
}

export interface ClusterProject {
	project_v2_id: string;
	origin: string;
	name: string;
	local_project_id: string | null;
	remote_only: boolean;
	eligible: ClusterProjectEligible[];
}

export interface ClusterProjectsResponse {
	projects: ClusterProject[];
}

/** Origin-keyed catalog of projects available across approved online installs. */
export async function listClusterProjects(): Promise<ClusterProjectsResponse> {
	return api.get<ClusterProjectsResponse>('/cluster/projects');
}
