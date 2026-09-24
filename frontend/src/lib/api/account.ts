/**
 * Shuffle account client (worker side).
 *
 * A worker links to exactly one Shuffle account through the account hub. The
 * link gives the account's other workers a peer for settings sync, gives the
 * phone app a worker to control, and enables remote browser access. Local and
 * LAN use never require an account.
 *
 * `GET /mobile/hub/connection` is the link state; `POST` starts (or reopens)
 * a ten-minute offer whose code the owner claims in the browser or with the
 * phone; `DELETE` signs this worker out. `/mobile/hub/account` reads and
 * completes the owning account's profile through the worker's own link.
 */

import { api } from './client';

export const ACCOUNT_CHANGED_EVENT = 'shuffle-account-changed';

function notifyAccountChanged() {
	if (typeof window !== 'undefined') window.dispatchEvent(new Event(ACCOUNT_CHANGED_EVENT));
}

export type AccountLinkState =
	| 'disconnected'
	| 'waiting'
	| 'connecting'
	| 'connected'
	| 'reconnecting'
	| 'locked'
	| 'unavailable'
	| (string & {});

export interface AccountConnection {
	signed_in: boolean;
	/** False while the saved account link is still being activated. Absent on older workers. */
	profile_ready?: boolean;
	status: AccountLinkState;
	server_id?: string | null;
	/** The public address of this worker once remote access is up. */
	url?: string | null;
	/** The one-time worker code while an offer is open. */
	code: string | null;
	expires_at?: number | string | null;
	/** Hub page that signs the owner in and links this worker. */
	authorization_url?: string | null;
	/** The same page opened on its create-account screen. */
	signup_url?: string | null;
	qr_svg: string | null;
	error: string | null;
	/**
	 * The account service refused this worker a remote connection because the
	 * account already uses all it may (`status` is then `limited`). The worker
	 * keeps working locally. Absent on older workers.
	 */
	refusal?: TunnelLimitRefusal | null;
}

/** The hub's `tunnel_limit` answer, as the worker reports it. */
export interface TunnelLimitRefusal {
	code: 'tunnel_limit';
	/** Remote connections this account may use. */
	limit: number;
	/** Remote connections its other workers use now. */
	used: number;
	/** Whether the account service counted the account as licensed. */
	licensed: boolean;
	/** What to tell the owner. */
	message: string;
}

/** Asks an open Settings card to show one of its tabs, e.g. `license`. */
export const OPEN_SETTINGS_TAB_EVENT = 'shuffle-open-settings-tab';

export function openSettingsTab(tab: string) {
	if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_TAB_EVENT, { detail: { tab } }));
}

export interface AccountWorker {
	id: string;
	name: string;
	url: string | null;
	/** True for the worker answering the request. */
	current: boolean;
}

export interface AccountProfile {
	account: { id: string | null; username: string; profile_complete: boolean };
	/** Every registered worker, independent of this worker's local network. */
	servers: AccountWorker[];
	/** Account-wide phone/browser metadata. Absent when the hub predates this inventory. */
	devices?: { id: string; name: string; platform: string; local_device_id?: string }[];
}

export async function getAccountConnection(): Promise<AccountConnection> {
	return api.get<AccountConnection>('/mobile/hub/connection', { cache: 'no-store' });
}

/** Start (or reopen) the offer that lets the owner claim this worker. */
export async function beginAccountConnection(workerName: string): Promise<AccountConnection> {
	return api.post<AccountConnection>('/mobile/hub/connection', { name: workerName.trim().slice(0, 80) || 'My worker' });
}

/** Sign this worker out of its account (other workers and phones keep theirs). */
export async function disconnectAccount(): Promise<AccountConnection> {
	const result = await api.delete<AccountConnection>('/mobile/hub/connection');
	notifyAccountChanged();
	return result as AccountConnection;
}

/** Bounded worker-side display cache; not authorization or account inventory. */
export async function getAccountDisplay(): Promise<Pick<AccountProfile, 'account'>> {
	return api.get<Pick<AccountProfile, 'account'>>('/mobile/hub/account/display', { cache: 'no-store' });
}

export async function getAccountProfile(): Promise<AccountProfile> {
	return api.get<AccountProfile>('/mobile/hub/account', { cache: 'no-store' });
}

/** Revoke an account registration, not a separate local pairing or network membership. */
export async function revokeAccountRegistration(kind: 'servers' | 'devices', id: string): Promise<{ ok: boolean; cleanup_pending?: boolean }> {
	return (await api.delete<{ ok: boolean; cleanup_pending?: boolean }>(`/mobile/hub/account/${kind}/${encodeURIComponent(id)}`))!;
}

export async function setAccountUsername(username: string): Promise<AccountProfile> {
	const profile = await api.patch<AccountProfile>('/mobile/hub/account', { username: username.trim() });
	notifyAccountChanged();
	return profile;
}

/** Mirrors the hub rule so the form can validate before a round trip. */
export const USERNAME_PATTERN = /^[a-z0-9][a-z0-9_.-]{2,63}$/;
export const USERNAME_HINT = 'Use 3–64 letters, numbers, underscores, periods or hyphens, starting with a letter or number.';

export function normalizeUsername(value: string): string {
	return value.trim().toLowerCase();
}
