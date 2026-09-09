/**
 * Worker-network API client.
 *
 * Every worker has a device identity and belongs to exactly one network.
 * Workers sync settings/preferences/profiles/keys with each other and each
 * session executes on one owner device. These endpoints power the Network
 * panel (Settings → Devices), the first-run wizard's network step and the
 * "fork here" offer the chat shows when a session's owner is unreachable.
 *
 * The runner picker keeps reading `GET /cluster/installs` (which returns the
 * same rows as `GET /network/runners` once enrolled) — see installs.ts.
 */

import { api } from './client';

// ─── Status ──────────────────────────────────────────────────────────────────

export interface NetworkDeviceIdentity {
	device_id: string;
	fingerprint: string;
	signing_public?: string;
	encryption_public?: string;
	incarnation?: number;
}

export type EnrollmentState = 'pending' | 'complete' | 'failed' | 'rejected' | 'expired' | (string & {});

/** The joiner-side record of an in-flight (or finished) join. */
export interface NetworkEnrollment {
	id: string;
	status: EnrollmentState;
	endpoint_url: string | null;
	inviter_fingerprint: string | null;
	/** This worker's own fingerprint (what the approver compares against). */
	fingerprint: string | null;
	error: string | null;
	created_at?: string | null;
	updated_at?: string | null;
}

export type BootstrapState = 'ready' | 'syncing' | (string & {});

export interface NetworkSummary {
	network_id: string;
	name: string;
	device_id: string;
	fingerprint: string;
	founder_device_id: string;
	generation: number;
	key_version: number;
	bootstrap_state: BootstrapState;
	member_count: number;
}

export interface NetworkMember {
	device_id: string;
	name: string;
	kind: string;
	platform: string | null;
	status: 'active' | 'revoked' | (string & {});
	generation: number;
	is_self: boolean;
	fingerprint: string | null;
	added_by: string | null;
	created_at: string | null;
	last_seen: string | null;
	endpoints: string[];
	capabilities: Record<string, unknown>;
	version: string | null;
	online: boolean;
	legacy_instance_id: string | null;
}

/** A join request parked on THIS worker (the inviter) awaiting approval. */
export interface PendingJoin {
	id: string;
	status: string;
	device_id: string;
	name: string;
	platform: string | null;
	fingerprint: string | null;
	legacy_instance_id: string | null;
	created_at: string | null;
	expires_at: string | null;
}

export interface SyncPeer {
	device_id: string;
	base_url: string | null;
	endpoints: string[];
	last_error: string | null;
	seconds_since_contact: number | null;
	online: boolean;
}

export interface SyncHole {
	origin_device_id: string;
	incarnation: number;
	first_pending: number;
	count: number;
	watermark: number | null;
}

export interface SyncStatus {
	records: Record<string, number>;
	ops: number;
	holes: SyncHole[];
	peers: SyncPeer[];
	conflicts: number;
}

export interface RecoveryStatus {
	configured: boolean;
	created_at: string | null;
	key_version: number | null;
}

/** A device that registered through the account; an online member admits it automatically. */
export interface HubPendingDevice {
	device_id: string;
	name: string | null;
	platform: string | null;
	signing_public: string;
	encryption_public?: string;
	legacy_instance_id?: string | null;
	created_at?: string | null;
}

/** What the hub knows about the account's network (for a worker waiting to be admitted). */
export interface AccountNetworkInfo {
	network_id: string | null;
	name: string | null;
	founder_device_id: string | null;
	devices: { device_id: string; name: string | null; platform: string | null; kind: string | null; status: string | null; version?: string | null }[];
	recovery_available: boolean;
}

export interface HubStatus {
	linked: boolean;
	server_id?: string | null;
	registered?: boolean;
	status?: string | null;
	cursor?: number;
	pushed_ingest_id?: number;
	backlog?: number;
	last_push_at?: string | null;
	last_pull_at?: string | null;
	last_error?: string | null;
	quota?: Record<string, unknown> | null;
	pending_devices?: HubPendingDevice[];
	/** Present while this worker waits on the account (status pending) or clashes with it (conflict). */
	account_network?: AccountNetworkInfo | null;
}

export interface NetworkStatus {
	device: NetworkDeviceIdentity;
	enrolled: boolean;
	platform: string | null;
	version: string | null;
	instance_id: string;
	/** Present (possibly null) only while NOT enrolled. */
	enrollment?: NetworkEnrollment | null;
	network?: NetworkSummary;
	members?: NetworkMember[];
	pending_joins?: PendingJoin[];
	sync?: SyncStatus;
	recovery?: RecoveryStatus;
	hub?: HubStatus;
}

export async function getNetworkStatus(): Promise<NetworkStatus> {
	return api.get<NetworkStatus>('/network/status', { cache: 'no-store' });
}

// ─── Create / members ────────────────────────────────────────────────────────

/** Found a new network with this worker as its first member (admin). 409 when already enrolled. */
export async function createNetwork(body: { name?: string; device_name?: string } = {}): Promise<{ network: NetworkSummary }> {
	const payload: Record<string, string> = {};
	if (body.name?.trim()) payload.name = body.name.trim();
	if (body.device_name?.trim()) payload.device_name = body.device_name.trim();
	return api.post<{ network: NetworkSummary }>('/network/create', payload);
}

export async function listNetworkMembers(): Promise<NetworkMember[]> {
	const response = await api.get<{ members: NetworkMember[] }>('/network/members');
	return response.members;
}

export async function renameNetworkMember(deviceId: string, name: string): Promise<NetworkMember> {
	return api.patch<NetworkMember>(`/network/members/${encodeURIComponent(deviceId)}`, { name: name.trim() });
}

export interface RevokeMemberResult {
	device_id: string;
	generation: number;
	key_version: number;
	/** The removed device's copies of provider keys are assumed compromised. */
	rotate_provider_keys_recommended: boolean;
}

/** Revoke a device (admin). Rotates the network key; the UI must warn about provider keys. */
export async function revokeNetworkMember(deviceId: string): Promise<RevokeMemberResult> {
	const result = await api.delete<RevokeMemberResult>(`/network/members/${encodeURIComponent(deviceId)}`);
	return result as RevokeMemberResult;
}

// ─── Conflicts ───────────────────────────────────────────────────────────────

export interface NetworkConflict {
	id: number;
	collection: string;
	record_id: string;
	losing_clock: unknown;
	losing_device_id: string;
	losing_record: Record<string, unknown> | null;
	winning_clock: unknown;
	winning_device_id: string;
	created_at: string;
	expires_at: string;
}

export async function listNetworkConflicts(): Promise<NetworkConflict[]> {
	const response = await api.get<{ conflicts: NetworkConflict[] }>('/network/conflicts');
	return response.conflicts;
}

export async function dismissNetworkConflict(id: number): Promise<void> {
	await api.delete(`/network/conflicts/${encodeURIComponent(String(id))}`);
}

// ─── Runners ─────────────────────────────────────────────────────────────────

export interface NetworkRunner {
	id: string;
	device_id: string;
	device_name: string;
	base_url: string | null;
	endpoints: string[];
	platform: string | null;
	version: string | null;
	capabilities: Record<string, unknown>;
	approved: true;
	online: boolean;
	is_self: boolean;
	legacy_instance_id: string | null;
}

export interface NetworkRunnersResponse {
	runners: NetworkRunner[];
	self_device_id: string | null;
	network_id: string | null;
	enrolled: boolean;
}

export async function listNetworkRunners(): Promise<NetworkRunnersResponse> {
	return api.get<NetworkRunnersResponse>('/network/runners');
}

// ─── Recovery key ────────────────────────────────────────────────────────────

export interface CreatedRecoveryKey {
	/** `A1-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-X` — shown ONCE, never stored. */
	recovery_key: string;
	created_at: string;
	key_version: number;
}

export async function getRecoveryStatus(): Promise<RecoveryStatus> {
	return api.get<RecoveryStatus>('/network/recovery');
}

/** Create (or rotate) the recovery key. The key in the response is never shown again. */
export async function createRecoveryKey(): Promise<CreatedRecoveryKey> {
	return api.post<CreatedRecoveryKey>('/network/recovery', {});
}

/** Restore the account's network onto this fresh worker with the recovery key (admin, signed in). */
export async function unlockWithRecoveryKey(recoveryKey: string, deviceName?: string): Promise<{ restored: boolean; network: NetworkSummary }> {
	const body: Record<string, string> = { recovery_key: recoveryKey.trim() };
	if (deviceName?.trim()) body.device_name = deviceName.trim();
	return api.post<{ restored: boolean; network: NetworkSummary }>('/network/recovery/unlock', body);
}

// ─── Inviting another worker (this worker is the inviter) ───────────────────

export interface JoinLink {
	url: string;
	endpoint: string;
	code: string;
	/** THIS worker's fingerprint — the joiner must compare it. */
	fingerprint: string;
	device_name: string | null;
}

export interface NetworkJoinGrant {
	grant: {
		id: string;
		purpose: 'network:join';
		expires_at: string;
		endpoint_url: string | null;
		[key: string]: unknown;
	};
	code: string;
	display_code: string;
	pairing_url: string | null;
	qr_svg: string | null;
	join: JoinLink | null;
}

/** Mint a one-time join code bound to one of this worker's endpoints. */
export async function createNetworkJoinGrant(endpointUrl: string): Promise<NetworkJoinGrant> {
	return api.post<NetworkJoinGrant>('/pairing/grants', { purpose: 'network:join', endpoint_url: endpointUrl });
}

export async function listPendingJoins(): Promise<PendingJoin[]> {
	const response = await api.get<{ joins: PendingJoin[] }>('/network/joins');
	return response.joins;
}

/** Admit a join that is still parked (the inviter admits on contact; this is the fallback). */
export async function approveJoin(joinId: string): Promise<{ join_id: string; status: string; device_id: string }> {
	return api.post(`/network/joins/${encodeURIComponent(joinId)}/approve`, {});
}

export async function rejectJoin(joinId: string): Promise<{ join_id: string; status: string }> {
	return api.post(`/network/joins/${encodeURIComponent(joinId)}/reject`, {});
}

// ─── Joining from another worker (this worker is fresh) ─────────────────────

export type EnrollInput =
	| { link: string; device_name?: string; replace?: boolean }
	| { endpoint_url: string; code: string; inviter_fingerprint?: string; device_name?: string; replace?: boolean };

export interface EnrollStarted {
	join_id: string;
	/** The inviter admits on contact; `pending` only means the response is still being collected. */
	status: 'pending' | 'approved' | (string & {});
	inviter: { device_id: string; fingerprint: string; name: string | null; network_name: string | null };
	your_fingerprint: string;
}

/**
 * Join the network behind an invite. The one-time code in the link is the
 * authorization; nobody approves on the other side. `replace` lets an
 * already-enrolled worker leave its own network for the invited one.
 */
export async function startEnrollment(input: EnrollInput): Promise<EnrollStarted> {
	const body: Record<string, string | boolean> = {};
	if ('link' in input) {
		body.link = input.link.trim();
	} else {
		body.endpoint_url = input.endpoint_url.trim();
		body.code = input.code.trim();
		if (input.inviter_fingerprint?.trim()) body.inviter_fingerprint = input.inviter_fingerprint.trim();
	}
	if (input.device_name?.trim()) body.device_name = input.device_name.trim();
	if (input.replace) body.replace = true;
	return api.post<EnrollStarted>('/network/enroll', body);
}

export async function getEnrollment(): Promise<{ enrollment: NetworkEnrollment | null; enrolled: boolean }> {
	return api.get('/network/enroll', { cache: 'no-store' });
}

export async function cancelEnrollment(): Promise<void> {
	await api.delete('/network/enroll');
}

// ─── Account (hub) devices ──────────────────────────────────────────────────

export async function listHubPendingDevices(): Promise<HubPendingDevice[]> {
	const response = await api.get<{ devices: HubPendingDevice[] }>('/network/hub/devices');
	return response.devices;
}

/** Admit a device that registered through the account (the sync loop does this on its own). */
export async function approveHubDevice(deviceId: string): Promise<{ device_id: string; status: string }> {
	return api.post(`/network/hub/devices/${encodeURIComponent(deviceId)}/approve`, {});
}

/** Leave this worker's own network and join the account's; the account's records win. */
export async function adoptAccountNetwork(deviceName?: string): Promise<{ adopted: boolean; status: string | null }> {
	const body: Record<string, string> = {};
	if (deviceName?.trim()) body.device_name = deviceName.trim();
	return api.post('/network/hub/adopt', body);
}

export async function rejectHubDevice(deviceId: string): Promise<{ device_id: string; status: string }> {
	return api.post(`/network/hub/devices/${encodeURIComponent(deviceId)}/reject`, {});
}

// ─── Fork on an offline owner ────────────────────────────────────────────────

export interface ForkHereResult {
	session_id: string;
	parent_session_id: string;
	context_chars: number;
}

/** Continue a session whose owning worker is unreachable: a new local session with the mirrored transcript. */
export async function forkSessionHere(sessionId: string): Promise<ForkHereResult> {
	return api.post<ForkHereResult>(`/network/sessions/${encodeURIComponent(sessionId)}/fork-here`, {});
}

// ─── Policy ("what syncs") ───────────────────────────────────────────────────

export interface PolicyCollection {
	collection: string;
	table: string;
	mode: string;
	keys: string[];
	fields: string[];
	encrypted: string[];
	local_fields: string[];
	schema_version: number;
	notes: string | null;
}

export interface PolicySettingRule {
	pattern: string;
	sharing: 'local' | 'shared' | (string & {});
}

export interface NetworkPolicy {
	collections: PolicyCollection[];
	settings: PolicySettingRule[];
}

export async function getNetworkPolicy(): Promise<NetworkPolicy> {
	return api.get<NetworkPolicy>('/network/policy');
}
