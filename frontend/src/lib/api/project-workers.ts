/**
 * Cross-worker project sync: which workers hold each project, and setting a
 * project up on another worker (clone a Git repository there, or create an
 * empty folder for a plain-folder project).
 *
 * Contract (all under /api/v1):
 *   GET    /projects/overview                    every logical project, with every approved worker's state
 *   GET    /projects/workers?<ref>               one project's per-worker detail (Workers tab)
 *   POST   /projects/setup                       start a setup (202)
 *   POST   /projects/clone                       create a project by cloning it here (202)
 *   GET    /projects/setup/{op_id}               one setup on this worker (status, progress, project id)
 *   POST   /projects/setup/{op_id}/cancel        best-effort cancel (409 once finished)
 *   DELETE /projects/workers/{install_id}?<ref>  remove one worker's project record (files untouched)
 *   PUT    /projects/catalog/repository?<ref>    point the project at another repository on every worker
 *   POST   /projects/catalog/forget?<ref>        take a project no worker holds off the list (nothing deleted)
 *
 * `<ref>` is exactly one of `project_v2_id` (preferred) or `project_id`.
 *
 * An older worker has none of these routes: `GET /projects/overview` then
 * lands on `GET /projects/{id}` and answers 404. Callers fall back to the
 * plain project list (fail open), so `getProjectsOverview` resolves `null`
 * for that case instead of throwing.
 */

import { api } from './client';
import type { BrowseResponse } from './filesystem';

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
	/** Opaque, target-issued revision; absent when retarget is unavailable. */
	folder_revision?: string | null;
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

/**
 * A comparison key for a Git remote in any spelling (HTTPS, SSH `git@host:o/r`,
 * with or without `.git`, credentials or a trailing slash): `host/owner/repo`
 * lower-cased. Mirrors the server's `display_remote`, so it also matches an
 * overview row's `remote`. Empty for a blank URL.
 */
export function remoteKey(url: string | null | undefined): string {
	let text = (url ?? '').trim();
	const scheme = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/(.*)$/.exec(text);
	if (scheme) {
		text = scheme[1].replace(/^[^@/]*@/, '');
	} else {
		const scp = /^(?:[^@/]+@)?([^:/]+):(?!\/)(.*)$/.exec(text);
		if (scp && scp[1].length > 1) text = `${scp[1]}/${scp[2]}`;
	}
	text = text.replace(/\/+$/, '');
	if (text.toLowerCase().endsWith('.git')) text = text.slice(0, -4);
	return text.toLowerCase();
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

function folderUrl(installId: string, ref: ProjectRef, suffix = ''): string {
	return `/projects/workers/${encodeURIComponent(installId)}/folder${suffix}?${refQuery(ref)}`;
}

export function browseWorkerFolder(installId: string, ref: ProjectRef, path?: string): Promise<BrowseResponse> {
	const query = path ? `&path=${encodeURIComponent(path)}` : '';
	return api.get<BrowseResponse>(folderUrl(installId, ref, '/browse') + query);
}

export function changeWorkerFolder(installId: string, ref: ProjectRef, folder: string, expected_revision: string) {
	return api.put<{ folder: string; folder_revision: string }>(folderUrl(installId, ref), { folder, expected_revision });
}

export function startProjectSetup(ref: ProjectRef, request: SetupRequest): Promise<SetupResponse> {
	return api.post<SetupResponse>('/projects/setup', { ...ref, mode: 'auto', ...request });
}

export interface CloneNewProjectRequest {
	id: string;
	name: string;
	clone_url: string;
	/** A folder name or path on this worker; omitted = `<workspace>/<id>`. */
	folder?: string;
}

export interface CloneNewProjectResponse {
	op_id: string;
	project_v2_id: string;
	/** The id the project gets once the clone succeeded. */
	project_id: string;
	status: string;
}

/** One setup on this worker; `project_id` is set once it is done. */
export interface SetupStatus extends ProjectSetupInfo {
	project_v2_id: string | null;
	project_id: string | null;
}

/**
 * Create a project by cloning a repository on this worker. The clone runs in
 * the background (poll `getSetupStatus`); the project is registered only once
 * it succeeded. 409 with `error_code: 'already_added'` (and `project_id`)
 * when the repository is already a project here; 409 with
 * `error_code: 'being_set_up'` (and `worker`) while another worker is
 * cloning it as a new project.
 */
export function cloneNewProject(request: CloneNewProjectRequest): Promise<CloneNewProjectResponse> {
	return api.post<CloneNewProjectResponse>('/projects/clone', request);
}

export function getSetupStatus(opId: string): Promise<SetupStatus> {
	return api.get<SetupStatus>(`/projects/setup/${encodeURIComponent(opId)}`);
}

export function cancelProjectSetup(opId: string): Promise<{ status: string }> {
	return api.post<{ status: string }>(`/projects/setup/${encodeURIComponent(opId)}/cancel`);
}

export async function removeProjectFromWorker(installId: string, ref: ProjectRef): Promise<void> {
	await api.delete(`/projects/workers/${encodeURIComponent(installId)}?${refQuery(ref)}`);
}

/** One worker's answer to a repository change. */
export interface RepositoryChangeWorker {
	install_id: string;
	device_name: string | null;
	is_self: boolean;
	/** updated / already: done. offline / pending: it switches when it next runs.
	 *  unsupported: that worker needs an update first. Anything else failed there. */
	status: 'updated' | 'already' | 'offline' | 'pending' | 'unsupported' | 'fetch_failed' | 'no_git' | 'error' | (string & {});
	ok: boolean;
	error?: string;
	branch?: string;
	copies?: number;
}

export interface RepositoryChangeResponse {
	project_v2_id: string;
	name: string | null;
	/** Display form of the new repository. */
	repository: string | null;
	remote_url: string;
	previous_repository: string | null;
	/** The previous repository stays reachable as the `upstream` remote. */
	kept_as_upstream: boolean;
	default_branch: string | null;
	/** A separate project for the new repository was folded into this one. */
	merged_duplicate: boolean;
	workers: RepositoryChangeWorker[];
	/** Workers with no copy; they clone the new repository if set up later. */
	workers_without_copy: (string | null)[];
}

/** Point the project at another Git repository (a new repo or a fork) on every worker. */
export function changeProjectRepository(ref: ProjectRef, remote_url: string, keep_upstream: boolean): Promise<RepositoryChangeResponse> {
	return api.put<RepositoryChangeResponse>(`/projects/catalog/repository?${refQuery(ref)}`, { remote_url, keep_upstream });
}

/** Take a project no worker holds off the Projects list. Its chats keep it. */
export function forgetProject(ref: ProjectRef): Promise<{ ok: boolean; project_v2_id: string }> {
	return api.post<{ ok: boolean; project_v2_id: string }>(`/projects/catalog/forget?${refQuery(ref)}`);
}
