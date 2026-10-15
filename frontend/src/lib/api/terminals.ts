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

export interface TerminalTarget { kind: string; worker_id?: string; grant_id?: string; name: string; available: boolean }

export interface TerminalTargets {
	worker: TerminalWorker;
	supported: boolean;
	unsupported_reason: string | null;
	targets: TerminalTarget[];
	shells: { name: string; default: boolean }[];
	default_shell: string | null;
	default_folder: { path: string; source: string } | null;
	limit: number;
	open: number;
}

export interface CreateTerminal { target?: Pick<TerminalTarget, 'kind' | 'worker_id' | 'grant_id'>; session_id?: string | null; cwd?: string | null; shell?: string | null; cols?: number; rows?: number }

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
	const detail = typeof body?.detail === 'string' ? body.detail : FALLBACK[response.status] ?? `Request failed (${response.status}).`;
	const code = typeof body?.code === 'string' ? body.code : `http_${response.status}`;
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
};
