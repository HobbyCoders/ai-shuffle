/**
 * In-app terminals REST client (#1945). Contract: docs/internal/terminals.md.
 *
 * Its own small fetch wrapper, because `ApiClient` drops the `code` field the
 * terminal routes return and the UI branches on it (`limit_reached`,
 * `chat_on_other_worker`, `terminal_busy`, ...). Terminal input and output
 * never pass through here: keystrokes go over the socket, and pasted text is
 * sent once and never kept.
 */
import { apiBase, withAuth } from './runtime';
import { recheckAuthOn401 } from './client';

export type TerminalState = 'starting' | 'live' | 'exited';

export interface TerminalInfo {
	id: string;
	target: { kind: string; worker_id?: string };
	shell: string;
	cwd: string;
	folder_source: string;
	session_id: string | null;
	state: TerminalState;
	exit_code: number | null;
	created_at: string;
	exited_at: string | null;
	busy: boolean;
	cols: number;
	rows: number;
	attached: number;
}

export interface TerminalWorker { id: string; name: string; platform: string; container?: boolean }

export interface TerminalList { terminals: TerminalInfo[]; open: number; running: number; limit: number; worker: TerminalWorker }

export interface TerminalTarget {
	kind: string; worker_id?: string; grant_id?: string; name: string; available: boolean;
	/** Peer workers only: why the worker is not usable right now (see PeerReasonCode). */
	reason_code?: string | null;
	reason?: string | null;
}

/** Peer-terminal availability codes on a target row (docs/internal/terminals.md). */
export type PeerReasonCode = 'peer_offline' | 'peer_terminals_disabled' | 'peer_unsupported_or_disabled' | 'not_supported'
	| 'https_required' | 'peer_unavailable';

/** What one device signature opens. Exactly these three fields are signed. */
export interface ProofTarget { session_id: string | null; cwd: string | null; shell: string | null }

/** A device's signature for one open. `payload` is signed exactly as sent. */
export interface DeviceProof { key_id: string; payload: string; signature: string }

/** The receiving worker's answer for this device (`POST /peer-challenge`). */
export interface PeerChallenge {
	/** `linked` carries the single-use challenge; `pending` and `unlinked` do not. */
	status: 'linked' | 'pending' | 'unlinked';
	receiver: string;
	via: string;
	challenge?: string;
	expires_in?: number;
}

/** A device waiting to link on this worker. Its code is never listed: the owner types it from the device. */
export interface PeerLinkRequest {
	request_id: string;
	label: string;
	/** The worker the device asked through. */
	worker_id: string;
	worker_name: string;
	age_seconds: number;
	expires_in: number;
}

/** A device linked on this worker. */
export interface LinkedDevice {
	key_id: string;
	label: string;
	worker_id: string;
	worker_name: string;
	linked_by: string;
	created_at: string;
	last_used_at: string | null;
}

export interface TerminalTargets {
	worker: TerminalWorker;
	supported: boolean;
	unsupported_reason: string | null;
	targets: TerminalTarget[];
	shells: { name: string; default: boolean }[];
	default_shell: string | null;
	default_folder: { path: string; source: string } | null;
	/** The chat's own worker, when that isn't this worker. */
	chat_worker_id?: string | null;
	limit: number;
	open: number;
}

export interface CreateTerminal { target?: Pick<TerminalTarget, 'kind' | 'worker_id' | 'grant_id'> & { proof?: DeviceProof }; session_id?: string | null; cwd?: string | null; shell?: string | null; cols?: number; rows?: number }

/** A refused request. `code` is the service code, or `http_<status>` for auth and other errors. */
export class TerminalApiError extends Error {
	constructor(message: string, readonly status: number, readonly code: string) {
		super(message);
		this.name = 'TerminalApiError';
	}
}

const FALLBACK: Record<number, string> = {
	401: 'Sign in again to use terminals.',
	403: 'Only the owner can use terminals.',
};

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
	const headers = init.body ? { 'Content-Type': 'application/json' } : undefined;
	let response: Response;
	try {
		response = await fetch(`${apiBase()}/terminals${path}`, withAuth({ ...init, headers }));
	} catch {
		throw new TerminalApiError("Can't reach the worker. Check the connection and try again.", 0, 'network');
	}
	if (response.ok) return (await response.json()) as T;
	const body = (await response.json().catch(() => null)) as { detail?: unknown; code?: unknown } | null;
	// Peer routes put an object in `detail`: `{code, message}` (503) or an
	// availability `{reason_code, reason}` (409). Keep the server's words.
	const inner = body?.detail && typeof body.detail === 'object' ? (body.detail as Record<string, unknown>) : null;
	const text = (value: unknown) => (typeof value === 'string' && value ? value : null);
	const detail = text(body?.detail) ?? text(inner?.message) ?? text(inner?.reason)
		?? FALLBACK[response.status] ?? `Request failed (${response.status}).`;
	const code = text(body?.code) ?? text(inner?.code) ?? text(inner?.reason_code) ?? `http_${response.status}`;
	if (response.status === 401) void recheckAuthOn401();
	throw new TerminalApiError(detail, response.status, code);
}

const sessionQuery = (sessionId?: string | null) => (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '');

export const terminalsApi = {
	list: (sessionId?: string | null) => request<TerminalList>(sessionQuery(sessionId)),
	targets: (sessionId?: string | null) => request<TerminalTargets>(`/targets${sessionQuery(sessionId)}`),
	create: (body: CreateTerminal) =>
		request<{ terminal: TerminalInfo }>('', { method: 'POST', body: JSON.stringify(body) }).then((r) => r.terminal),
	get: (id: string) => request<{ terminal: TerminalInfo }>(`/${encodeURIComponent(id)}`).then((r) => r.terminal),
	close: (id: string) => request<{ closed: boolean }>(`/${encodeURIComponent(id)}`, { method: 'DELETE' }),
	restart: (id: string) =>
		request<{ terminal: TerminalInfo }>(`/${encodeURIComponent(id)}/restart`, { method: 'POST' }).then((r) => r.terminal),
	paste: (id: string, text: string) =>
		request<{ mode: 'bracketed' | 'plain'; terminal: TerminalInfo }>(`/${encodeURIComponent(id)}/paste`, {
			method: 'POST',
			body: JSON.stringify({ text }),
		}),

	// ── Peer terminals: this device's link, and the devices linked here ──
	/** Through this worker, ask `workerId` whether this device is linked, for a fresh challenge. */
	peerChallenge: (workerId: string, keyId: string) =>
		request<PeerChallenge>('/peer-challenge', { method: 'POST', body: JSON.stringify({ worker_id: workerId, key_id: keyId }) }),
	/** Through this worker, ask `workerId` to expect this device. The owner approves it there. */
	requestPeerLink: (workerId: string, publicKey: string, label: string) =>
		request<{ status: string; key_id: string; expires_in?: number }>('/peer-links', { method: 'POST', body: JSON.stringify({ worker_id: workerId, public_key: publicKey, label }) }),
	/** On the receiver: devices waiting to link here, and devices linked. */
	deviceLinks: () => request<{ links: LinkedDevice[]; requests: PeerLinkRequest[] }>('/device-links'),
	/** On the receiver: link the device whose code the owner typed. */
	approveDeviceLink: (code: string) =>
		request<LinkedDevice>('/device-links/approve', { method: 'POST', body: JSON.stringify({ code }) }),
	declineDeviceLink: (id: string) =>
		request<{ declined?: boolean }>(`/device-links/requests/${encodeURIComponent(id)}`, { method: 'DELETE' }),
	/** On the receiver: unlink a device. Any shell it has open here closes. */
	revokeDeviceLink: (keyId: string) =>
		request<{ revoked?: boolean }>(`/device-links/${encodeURIComponent(keyId)}`, { method: 'DELETE' }),
};
