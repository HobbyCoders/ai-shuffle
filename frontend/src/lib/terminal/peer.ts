/**
 * Terminals on another worker (#196, #674): the pure rules the picker, the link
 * card and the receiving worker's device list share. Contract:
 * docs/internal/terminals.md, "Peer-terminal API (UI contract)".
 *
 * A peer shell never opens on the requesting worker's word. This device signs
 * each open with its own key, and the receiving worker checks that against the
 * device it linked once, after the owner entered the device's code there.
 */
import type { TerminalTarget } from '$lib/api/terminals';
import { TerminalApiError } from '$lib/api/terminals';

export const PROOF_PURPOSE = 'ai-shuffle-peer-terminal-v1';
export const ACTION_OPEN = 'terminal.open';
export const ACTION_OPEN_AND_PASTE = 'terminal.open_and_paste';
export type PeerAction = typeof ACTION_OPEN | typeof ACTION_OPEN_AND_PASTE;

/** A link request waits on the receiving worker for at most this long. */
export const LINK_SECONDS = 600;

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
	peer_offline: { label: 'Offline', tone: 'off' },
	peer_terminals_disabled: { label: 'Turned off', tone: 'off' },
	peer_unsupported_or_disabled: { label: 'Not supported', tone: 'off' },
	not_supported: { label: 'No shell', tone: 'off' },
	https_required: { label: 'Needs HTTPS', tone: 'warn' },
	peer_unavailable: { label: 'Not answering', tone: 'warn' },
};

export function peerChip(target: TerminalTarget): { label: string; tone: Tone } {
	if (target.available) return { label: 'Ready', tone: 'ok' };
	return CHIP[target.reason_code ?? ''] ?? { label: 'Unavailable', tone: 'off' };
}

/** Whether the owner can pick this row. `keyProblem` is why this browser can't sign. */
export function selectable(target: TerminalTarget, keyProblem?: string | null): boolean {
	return target.available && !keyProblem;
}

/** The full reason under a row, in plain words. */
export function peerReason(target: TerminalTarget): string | null {
	if (target.available) return null;
	return target.reason || 'This worker is unavailable right now.';
}

/**
 * Chip and reason for a peer row, given this browser and the live link flow
 * (`phase`) for it. A row this browser can't sign for says so, and a row
 * waiting for its link says what the card under the list says.
 */
export function peerRow(target: TerminalTarget, phase?: string | null, keyProblem?: string | null): { chip: { label: string; tone: Tone }; reason: string | null } {
	if (target.available && keyProblem) return { chip: { label: 'Needs a secure connection', tone: 'warn' }, reason: keyProblem };
	if (phase === 'linking') return { chip: { label: 'Linking', tone: 'wait' }, reason: `Waiting for the code to be entered on ${target.name}.` };
	return { chip: peerChip(target), reason: peerReason(target) };
}

/** "1:54" for a countdown; "0:00" once it runs out. */
export function countdown(seconds: number): string {
	const s = Math.max(0, Math.ceil(seconds));
	return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** "just now", "12s ago", "1m 4s ago": how old a request is on the receiver. */
export function age(seconds: number): string {
	const s = Math.max(0, Math.floor(seconds));
	if (s < 3) return 'just now';
	if (s < 60) return `${s}s ago`;
	return `${Math.floor(s / 60)}m ${s % 60}s ago`;
}

/** The code in groups of four, so it is easy to read and type. */
export function codeGroups(code: string): string[] {
	return (code || '').toUpperCase().match(/.{1,4}/g) ?? [];
}

/** Why a request to this worker failed, for the link card. */
export function requestErrorMessage(error: unknown, name: string): string {
	if (error instanceof TerminalApiError) {
		if (error.status === 404) return `${name} is no longer a known worker on this network.`;
		if (error.status === 429) return `Too many devices are waiting to link to ${name}. Wait for one to finish or expire.`;
		if (error.code === 'https_required') return `${name} needs an authenticated HTTPS route before it can host a terminal.`;
		if (error.code === 'peer_unavailable') return `${name} didn't answer or refused the request. Check that it is online and try again.`;
		if (error.code === 'peer_terminals_disabled') return 'Terminals on other workers are turned off. Turn them on in Settings on both workers.';
		return error.message;
	}
	return `Couldn't reach ${name}. Try again.`;
}

/**
 * Why a signed open failed. A rejected folder (`bad_folder`) is not here: the
 * picker asks for another folder instead.
 */
export function openErrorMessage(code: string, message: string, name: string): string {
	switch (code) {
		case 'device_link_required': return `This device is no longer linked to ${name}. Open it again to link.`;
		case 'device_proof_expired': return `That request to ${name} ran out before it arrived. Try again.`;
		case 'device_proof_invalid': return `${name} refused this device's signature. Try again.`;
		case 'peer_unavailable': return `${name} stopped answering before the shell opened.`;
		case 'limit_reached': return message || 'Too many terminals are open. Close one first.';
		default: return message || `The shell on ${name} didn't open.`;
	}
}
