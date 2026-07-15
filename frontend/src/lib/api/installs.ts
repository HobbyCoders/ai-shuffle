/**
 * Installs (cluster) API client.
 *
 * On a shared database every AI Shuffle install self-registers into the
 * `installs` registry. These endpoints power the Devices panel and the cloud
 * device picker — listing installs, renaming/removing a device, and inspecting
 * or rotating the shared cluster secret.
 */

import { api } from './client';

export interface Install {
	id: string; // = instance_id
	device_name: string;
	base_url: string | null; // null = view-only (no peer control / offload target)
	platform: string | null;
	version: string | null;
	capabilities: Record<string, unknown>;
	last_seen: string | null;
	online: boolean; // derived server-side from last_seen
	is_self?: boolean; // annotated by GET /cluster/installs
	created_at?: string;
	updated_at?: string;
}

export interface InstallsResponse {
	installs: Install[];
	self_instance_id: string;
}

export interface ClusterSecretStatus {
	configured: boolean;
	env_pinned: boolean; // pinned via CLUSTER_SECRET env (regenerate is a no-op)
}

export interface AdvertiseStatus {
	url: string | null; // current peer-reachable URL (null = view-only)
	source: 'config' | 'guess' | 'none';
	env_pinned: boolean; // pinned via CLUSTER_ADVERTISE_URL env (override ignored)
	override: string | null; // the stored admin override, if any
	instance_id: string;
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

/** Report how the cluster secret is sourced — never returns the secret itself. */
export async function getClusterSecretStatus(): Promise<ClusterSecretStatus> {
	return api.get<ClusterSecretStatus>('/cluster/secret/status');
}

/** Rotate the auto-generated cluster secret (no-op when env-pinned). */
export async function regenerateClusterSecret(): Promise<{ ok: boolean; env_pinned: boolean }> {
	return api.post<{ ok: boolean; env_pinned: boolean }>('/cluster/secret/regenerate', {});
}

/** This install's advertised peer address (and override status). */
export async function getAdvertiseStatus(): Promise<AdvertiseStatus> {
	return api.get<AdvertiseStatus>('/cluster/advertise');
}

/** Override the URL this install advertises to peers (blank = auto-detect). */
export async function setAdvertiseUrl(base_url: string): Promise<AdvertiseStatus> {
	return api.put<AdvertiseStatus>('/cluster/advertise', { base_url });
}

export interface AdvertiseReachability {
	ok: boolean;
	url: string | null;
	source: 'config' | 'guess' | 'none';
	detail: string;
}

/** Self-test the advertised address (resolves + answers its health route).
 *  Catches unresolvable host names / wrong ports before peers hit them. */
export async function testAdvertiseReachability(): Promise<AdvertiseReachability> {
	return api.post<AdvertiseReachability>('/cluster/advertise/test', {});
}
