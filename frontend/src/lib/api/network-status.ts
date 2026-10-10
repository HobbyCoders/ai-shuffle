/**
 * Pure helpers for the worker-network UI.
 *
 * No Svelte, no fetch: state-pill derivation from a status payload,
 * fingerprint formatting (matching `app.network.crypto.fingerprint`), join
 * link detection and small time formatters. Unit-tested in
 * network-status.test.ts.
 */

import type { NetworkStatus } from './network';

// ─── State pill ──────────────────────────────────────────────────────────────

export type NetworkUiState = 'ready' | 'syncing' | 'waiting' | 'needs-setup' | 'offline';

export const NETWORK_STATE_LABELS: Record<NetworkUiState, string> = {
	ready: 'Ready',
	syncing: 'Syncing',
	waiting: 'Joining',
	'needs-setup': 'Needs local setup',
	offline: 'Offline'
};

export const NETWORK_STATE_HINTS: Record<NetworkUiState, string> = {
	ready: 'This worker is enrolled and in sync with its network.',
	syncing: 'This worker is still catching up on the network history.',
	waiting: 'Joining: another worker on the network admits this one as soon as it is online.',
	'needs-setup': 'This worker does not belong to a network yet.',
	offline: 'The worker did not answer — showing the last saved data.'
};

/**
 * Derive the state pill from a status payload.
 *
 * `stale` means the last status fetch failed (the panel keeps the previous
 * payload so the user still sees saved data) — that always wins as Offline.
 */
export function deriveNetworkState(status: NetworkStatus | null | undefined, stale = false): NetworkUiState {
	if (stale) return 'offline';
	if (!status) return 'offline';
	if (!status.enrolled) {
		if (status.enrollment?.status === 'pending' || status.enrollment?.status === 'syncing') return 'waiting';
		if (status.hub?.linked && status.hub.status === 'pending') return 'waiting';
		return 'needs-setup';
	}
	// The account always wins: a worker whose own network clashes with the
	// account's is switching to the account's network right now.
	if (status.hub?.linked && status.hub.status === 'conflict') return 'waiting';
	if (status.network && status.network.bootstrap_state !== 'ready') return 'syncing';
	if ((status.sync?.holes?.length ?? 0) > 0) return 'syncing';
	if (status.sync?.syncing || status.sync?.peers?.some((peer) => peer.syncing)) return 'syncing';
	return 'ready';
}

// ─── First copy ──────────────────────────────────────────────────────────────

export interface FirstCopyNotice {
	title: string;
	message: string;
	severity: 'info' | 'warning';
	/** 0..1 while pages download; null when there is no count to show. */
	fraction: number | null;
}

/** `4.7 of 8.5 GB`: both sizes in the total's unit. */
export function copySize(done: number, total: number): string {
	const gb = 1024 ** 3;
	const mb = 1024 ** 2;
	const [unit, label] = total >= gb ? [gb, 'GB'] : [mb, 'MB'];
	return `${(done / unit).toFixed(1)} of ${(total / unit).toFixed(1)} ${label}`;
}

/**
 * What to tell the user while this worker receives its first copy of the
 * network's data. Until it finishes there are no sessions and no other
 * workers to show, so the app must say the copy is running. Null once the
 * worker is ready (or it is not in a network).
 */
export function firstCopyNotice(status: NetworkStatus | null | undefined): FirstCopyNotice | null {
	if (!status?.enrolled || !status.network || status.network.bootstrap_state === 'ready') return null;
	const copy = status.hub?.first_copy;
	const title = 'Copying your data to this worker';
	if (!copy) {
		return {
			title,
			message: status.hub?.last_error || 'Waiting for another worker to copy your data to this one.',
			severity: 'info',
			fraction: null
		};
	}
	const peer = copy.peer_name || 'another worker';
	switch (copy.phase) {
		case 'downloading': {
			const total = copy.pages_total;
			if (!total) break;
			const size = copy.bytes_total ? ` (${copySize(copy.bytes_done, copy.bytes_total)})` : '';
			return {
				title,
				message: `Copying your data from ${peer}: ${copy.pages_done.toLocaleString('en-US')} of ${total.toLocaleString('en-US')} pages${size}.`,
				severity: 'info',
				fraction: Math.min(1, copy.pages_done / total)
			};
		}
		case 'applying':
		case 'done': {
			const total = copy.apply_total ?? 0;
			const done = Math.min(copy.apply_done ?? 0, total);
			return {
				title,
				message: `Saving the data copied from ${peer}. Sessions and workers appear when this finishes.`,
				severity: 'info',
				fraction: total ? done / total : null
			};
		}
		case 'failed':
			return {
				title,
				message:
					`Copying from ${peer} stopped: ${(copy.error || 'unknown error').replace(/[.\s]+$/, '')}. ` +
					(copy.pages_done > 0
						? `It will retry on its own and keep the ${copy.pages_done.toLocaleString('en-US')} pages already copied.`
						: 'It will retry on its own.'),
				severity: 'warning',
				fraction: null
			};
	}
	return { title, message: `Waiting for ${peer} to prepare a copy of your data.`, severity: 'info', fraction: null };
}

/**
 * A worker that just joined is usable before its transcripts arrive: they
 * download in the background, newest chats first, and a chat the user opens
 * goes to the front. Null when nothing is left to download.
 */
export function transcriptNotice(status: NetworkStatus | null | undefined): FirstCopyNotice | null {
	if (!status?.enrolled || status.network?.bootstrap_state !== 'ready') return null;
	const copy = status.hub?.transcripts;
	if (!copy || !copy.sessions_total) return null;
	const peer = copy.peer_name || 'another worker';
	const count = `${copy.sessions_done.toLocaleString('en-US')} of ${copy.sessions_total.toLocaleString('en-US')} sessions`;
	const fraction = Math.min(1, copy.sessions_done / copy.sessions_total);
	if (copy.error) {
		return {
			title: 'Downloading transcripts',
			message: `Downloading transcripts from ${peer} paused at ${count}: ${copy.error.replace(/[.\s]+$/, '')}. It will retry on its own.`,
			severity: 'warning',
			fraction
		};
	}
	return {
		title: 'Downloading transcripts',
		message: `Downloading transcripts from ${peer}: ${count}. Older chats fill in as they arrive; a chat you open is fetched first.`,
		severity: 'info',
		fraction
	};
}

/** Devices this worker is admitting right now: joins parked here plus devices arriving through the account. */
export function joiningDeviceCount(status: NetworkStatus | null | undefined): number {
	if (!status?.enrolled) return 0;
	return (status.pending_joins?.length ?? 0) + (status.hub?.pending_devices?.length ?? 0);
}

/** `/pair#token=…` (a phone/browser pairing link) is not a worker invite. */
export function looksLikePairingLink(value: string): boolean {
	return /\/pair#token=/.test(value.trim());
}

// ─── Fingerprints ────────────────────────────────────────────────────────────

/** `66687aadf862bd776c8fc18b8e9f8e20…` → `6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20` (first 32 hex chars). */
export function formatFingerprint(hex: string): string {
	const clean = normalizeFingerprint(hex).slice(0, 32);
	const groups: string[] = [];
	for (let i = 0; i < clean.length; i += 4) groups.push(clean.slice(i, i + 4));
	return groups.join('-');
}

/** Lower-case hex only — what two fingerprints are compared on. */
export function normalizeFingerprint(value: string | null | undefined): string {
	return (value ?? '').toLowerCase().replace(/[^0-9a-f]/g, '');
}

/** True when two displayed fingerprints denote the same 128-bit value. */
export function fingerprintsMatch(a: string | null | undefined, b: string | null | undefined): boolean {
	const x = normalizeFingerprint(a);
	const y = normalizeFingerprint(b);
	return x.length === 32 && x === y;
}

/** A pasted fingerprint is usable once it carries the full 32 hex chars. */
export function isCompleteFingerprint(value: string | null | undefined): boolean {
	return normalizeFingerprint(value).length === 32;
}

/** base64url (unpadded) → bytes. Rejects standard-alphabet or padded input like the server does. */
export function b64uDecode(text: string): Uint8Array {
	if (typeof text !== 'string' || /[+/=\s]/.test(text)) throw new Error('Invalid base64url text');
	const padded = text + '='.repeat((4 - (text.length % 4)) % 4);
	const binary = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
	const out = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
	return out;
}

function toHex(bytes: ArrayBuffer | Uint8Array): string {
	return Array.from(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes))
		.map((b) => b.toString(16).padStart(2, '0'))
		.join('');
}

/**
 * Fingerprint of a base64url-encoded signing public key: first 32 hex chars of
 * SHA-256, grouped 8×4 with dashes — byte-for-byte what the server prints for
 * its members, so an account-pending device can be compared before approval.
 */
export async function fingerprintFromPublicKey(
	signingPublicB64u: string,
	subtle: SubtleCrypto | null | undefined = globalThis.crypto?.subtle
): Promise<string> {
	if (!subtle) throw new Error('WebCrypto is unavailable in this context');
	const raw = b64uDecode(signingPublicB64u);
	const digest = await subtle.digest('SHA-256', raw as BufferSource);
	return formatFingerprint(toHex(digest));
}

// ─── Join links ──────────────────────────────────────────────────────────────

/** `shuffle://network/join?endpoint=…&code=…&fp=…` (or an https mirror of it). */
export function looksLikeJoinLink(value: string): boolean {
	const text = value.trim();
	return text.startsWith('shuffle://') || text.includes('network/join?');
}

/** Client-side parse of a join link so the manual fields can be prefilled. Null when not a link. */
export function parseJoinLink(value: string): { endpoint: string; code: string; fingerprint: string } | null {
	if (!looksLikeJoinLink(value)) return null;
	const text = value.trim().replace(/^shuffle:\/\//, 'https://');
	try {
		const url = new URL(text);
		return {
			endpoint: url.searchParams.get('endpoint') ?? '',
			code: url.searchParams.get('code') ?? '',
			fingerprint: url.searchParams.get('fp') ?? ''
		};
	} catch {
		return null;
	}
}

// ─── Time ────────────────────────────────────────────────────────────────────

export function secondsUntil(iso: string | null | undefined, now = Date.now()): number {
	if (!iso) return 0;
	const ms = new Date(iso).getTime();
	if (!Number.isFinite(ms)) return 0;
	return Math.max(0, Math.ceil((ms - now) / 1000));
}

/** `125` → `2:05`. */
export function formatCountdown(seconds: number): string {
	const s = Math.max(0, Math.floor(seconds));
	return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
	if (!iso) return 'never';
	const ms = new Date(iso).getTime();
	if (!Number.isFinite(ms)) return 'never';
	const diff = now - ms;
	if (diff < 60_000) return 'just now';
	if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
	if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
	if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)}d ago`;
	return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** Seconds since a peer answered → short label for the peer list. */
export function contactAge(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined) return 'no contact yet';
	if (seconds < 5) return 'just now';
	if (seconds < 60) return `${Math.round(seconds)}s ago`;
	if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
	return `${Math.floor(seconds / 3600)}h ago`;
}
