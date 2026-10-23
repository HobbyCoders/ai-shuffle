/**
 * Owner-approved terminals on a peer worker (#196): the pure rules the picker,
 * the pending card and the recipient's approval list share. Contract:
 * docs/internal/terminals.md, "Peer-terminal API (UI contract)".
 *
 * A peer target never opens on the source's word alone. The owner requests
 * one shell, approves it directly on the other worker after comparing the
 * six-character code, and the source opens it once.
 */
import type { TerminalTarget } from '$lib/api/terminals';
import { TerminalApiError } from '$lib/api/terminals';

/** The one actionable "unavailable" reason: ask the other worker's owner. */
export const APPROVAL_REQUIRED = 'recipient_approval_required';

/** Pending requests last two minutes on the recipient; approvals at most ten. */
export const PENDING_SECONDS = 120;
export const APPROVED_SECONDS = 600;

/** What opening returned: lets a peer open show its own failure in the picker. */
export type OpenResult = { ok: true } | { ok: false; code: string; message: string };

/** The local worker is always the first target row the server sends. */
export function localWorkerId(targets: TerminalTarget[] | undefined): string | undefined {
	return targets?.[0]?.kind === 'worker' ? targets[0].worker_id : undefined;
}

/** A worker row that is not this worker. */
export function isPeerTarget(target: TerminalTarget | undefined, localId: string | undefined): boolean {
	return !!target && target.kind === 'worker' && !!target.worker_id && target.worker_id !== localId;
}

export type Tone = 'ok' | 'wait' | 'warn' | 'bad' | 'off';

/** Short chip label for a peer row. Never the only signal: the reason line says it in full. */
const CHIP: Record<string, { label: string; tone: Tone }> = {
	[APPROVAL_REQUIRED]: { label: 'Needs approval', tone: 'wait' },
	peer_offline: { label: 'Offline', tone: 'off' },
	peer_terminals_disabled: { label: 'Turned off', tone: 'off' },
	peer_unsupported_or_disabled: { label: 'Not supported', tone: 'off' },
	not_supported: { label: 'No shell', tone: 'off' },
	https_required: { label: 'Needs HTTPS', tone: 'warn' },
	peer_unavailable: { label: 'Not answering', tone: 'warn' },
};

export function peerChip(target: TerminalTarget): { label: string; tone: Tone } {
	if (target.available) return target.authorization_id ? { label: 'Approved', tone: 'ok' } : { label: 'Ready', tone: 'ok' };
	return CHIP[target.reason_code ?? ''] ?? { label: 'Unavailable', tone: 'off' };
}

/** Whether the owner can pick this row: usable now, or usable after an approval. */
export function selectable(target: TerminalTarget): boolean {
	return target.available || target.reason_code === APPROVAL_REQUIRED;
}

/** The full reason under a row, in plain words. */
export function peerReason(target: TerminalTarget): string | null {
	if (target.available) return target.authorization_id ? 'Approved for one shell. Open it below.' : null;
	if (target.reason_code === APPROVAL_REQUIRED) return `Approve on ${target.name} to open one shell.`;
	return target.reason || 'This worker is unavailable right now.';
}

/** "1:54" for a countdown; "0:00" once it runs out. */
export function countdown(seconds: number): string {
	const s = Math.max(0, Math.ceil(seconds));
	return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** "just now", "12s ago", "1m 4s ago": how old a request is on the recipient. */
export function age(seconds: number): string {
	const s = Math.max(0, Math.floor(seconds));
	if (s < 3) return 'just now';
	if (s < 60) return `${s}s ago`;
	return `${Math.floor(s / 60)}m ${s % 60}s ago`;
}

/** The code in two groups of three, so it is easy to read aloud and compare. */
export function codeGroups(code: string): [string, string] {
	const clean = (code || '').toUpperCase();
	return [clean.slice(0, 3), clean.slice(3)];
}

/** Why a request from this worker failed, for the pending card. */
export function requestErrorMessage(error: unknown, name: string): string {
	if (error instanceof TerminalApiError) {
		if (error.status === 404) return `${name} is no longer a known worker on this network.`;
		if (error.status === 429) return `Too many open requests for ${name}. Cancel one or wait for it to expire.`;
		if (error.code === 'https_required') return `${name} needs an authenticated HTTPS route before it can host a terminal.`;
		if (error.code === 'peer_unavailable') return `${name} didn't answer or refused the request. Check that it is online and try again.`;
		if (error.code === 'peer_terminals_disabled') return 'Peer terminals are turned off. Turn them on in owner policy on both workers.';
		return error.message;
	}
	return `Couldn't reach ${name}. Try again.`;
}

/**
 * Why opening an approved peer shell failed. A rejected folder (`bad_folder`)
 * is not here: it spends nothing, so the picker keeps the approval and asks
 * for another folder instead.
 */
export function openErrorMessage(code: string, message: string, name: string): string {
	switch (code) {
		case APPROVAL_REQUIRED: return `The approval on ${name} expired or was revoked.`;
		case 'peer_unavailable': return `${name} stopped answering before the shell opened.`;
		case 'limit_reached': return message || 'Too many terminals are open. Close one first.';
		default: return message || `The shell on ${name} didn't open.`;
	}
}
