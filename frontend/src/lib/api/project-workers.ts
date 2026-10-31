/**
 * Cross-worker project sync: which workers hold each project, and setting a
 * project up on another worker (clone a Git repository there, or create an
 * empty folder for a plain-folder project).
 *
 * Contract (all under /api/v1):
 *   GET    /projects/overview                    every logical project, with every approved worker's state
 *   GET    /projects/workers?<ref>               one project's per-worker detail (Workers tab)
 *   POST   /projects/setup                       start a setup (202)
 *   POST   /projects/setup/{op_id}/cancel        best-effort cancel (409 once finished)
 *   DELETE /projects/workers/{install_id}?<ref>  remove one worker's project record (files untouched)
 *
 * `<ref>` is exactly one of `project_v2_id` (preferred) or `project_id`.
 *
 * An older worker has none of these routes: `GET /projects/overview` then
 * lands on `GET /projects/{id}` and answers 404. Callers fall back to the
 * plain project list (fail open), so `getProjectsOverview` resolves `null`
 * for that case instead of throwing.
 */

import { api } from './client';

/** One worker's relation to a project. */
export type WorkerProjectState =
	| 'ready'
	| 'missing'
	| 'setting_up'
	| 'queued_offline'
	| 'failed'
	| 'offline_has'
	| 'offline_missing'
	| 'unknown';

/** Stable failure classes; each comes with a plain-English `error` too. */
export type SetupErrorCode =
	| 'github_auth'
	| 'network'
	| 'folder_exists'
	| 'not_found'
	| 'offline'
	| 'unsupported'
	| 'internal';

export interface ProjectSetupInfo {
	op_id: string;
	status: 'queued' | 'queued_offline' | 'running' | 'done' | 'error' | 'cancelled' | (string & {});
	/** 0-100, or null when the step cannot report progress. */
	progress: number | null;
	error_code?: SetupErrorCode | (string & {}) | null;
	error?: string | null;
}

export interface ProjectWorker {
	install_id: string;
	device_name: string;
	is_self: boolean;
	online: boolean;
	approved: boolean;
	state: WorkerProjectState | (string & {});
	setup: ProjectSetupInfo | null;
}

export type ProjectKind = 'git' | 'folder';

export interface ProjectOverviewRow {
	project_v2_id: string;
	/** This worker's legacy project id, or null when it is not on this worker. */
	local_project_id: string | null;
	name: string;
	kind: ProjectKind;
	/** Display form (no credentials), e.g. github.com/owner/repo. */
	remote: string | null;
	default_branch: string | null;
	session_count: number;
	workers: ProjectWorker[];
}

export interface ProjectsOverviewResponse {
	projects: ProjectOverviewRow[];
}

export interface ProjectWorkerDetail extends ProjectWorker {
	/** Owner/admin only; null for API users. */
	folder: string | null;
	/** Where a setup would put it on that worker. */
	suggested_folder: string | null;
	branch: string | null;
	dirty: boolean | null;
	changed_files: number | null;
	last_seen: string | null;
}

export interface ProjectWorkersResponse {
	project_v2_id: string;
	name: string;
	kind: ProjectKind;
	remote: string | null;
	/** The folder name a setup uses by default (the source folder's last segment). */
	default_folder: string;
	workers: ProjectWorkerDetail[];
}

export type ProjectRef = { project_v2_id: string } | { project_id: string };

export interface SetupRequest {
	install_id: string;
	/** A folder name or a path on the target; omitted = the default folder. */
	folder?: string;
	mode?: 'auto' | 'adopt';
}

export interface SetupResponse {
	op_id: string;
	install_id: string;
	status: 'queued' | 'queued_offline' | 'done' | (string & {});
}

/** Build a ref from what a caller holds, preferring the logical id. */
export function projectRef(projectV2Id?: string | null, projectId?: string | null): ProjectRef | null {
	if (projectV2Id) return { project_v2_id: projectV2Id };
	if (projectId) return { project_id: projectId };
	return null;
}

export function refQuery(ref: ProjectRef): string {
	return 'project_v2_id' in ref
		? `project_v2_id=${encodeURIComponent(ref.project_v2_id)}`
		: `project_id=${encodeURIComponent(ref.project_id)}`;
}

/** True for an answer that means "this worker has no such route" (an older worker). */
export function isUnsupported(error: unknown): boolean {
	const status = (error as { status?: number } | null)?.status;
	return status === 404 || status === 405;
}

/**
 * The merged Projects-page list. `null` when this worker predates the
 * endpoint (fall back to the plain list with no chips).
 */
export async function getProjectsOverview(): Promise<ProjectOverviewRow[] | null> {
	try {
		const res = await api.get<ProjectsOverviewResponse>('/projects/overview');
		return Array.isArray(res?.projects) ? res.projects : [];
	} catch (error) {
		if (isUnsupported(error)) return null;
		throw error;
	}
}

export function getProjectWorkers(ref: ProjectRef, options?: { signal?: AbortSignal }): Promise<ProjectWorkersResponse> {
	return api.get<ProjectWorkersResponse>(`/projects/workers?${refQuery(ref)}`, options);
}

export function startProjectSetup(ref: ProjectRef, request: SetupRequest): Promise<SetupResponse> {
	return api.post<SetupResponse>('/projects/setup', { ...ref, mode: 'auto', ...request });
}

export function cancelProjectSetup(opId: string): Promise<{ status: string }> {
	return api.post<{ status: string }>(`/projects/setup/${encodeURIComponent(opId)}/cancel`);
}

export async function removeProjectFromWorker(installId: string, ref: ProjectRef): Promise<void> {
	await api.delete(`/projects/workers/${encodeURIComponent(installId)}?${refQuery(ref)}`);
}
