/**
 * System > Memory > Memory health: the worker's own check of long-term memory
 * (GET /memory/health, app/core/fluid_memory.py owner_health). Read-only: the
 * worker repairs what it safely can by itself; this only reports.
 */
import { api } from '$lib/api/client';
import type { UpdateResultRow } from './updates-state.svelte';
import { relativeAgo } from '../integrations/api-users.svelte';

export interface MemorySignal {
	id: string;
	label: string;
	state: 'healthy' | 'attention';
	reason: string;
	/**
	 * A healthy signal whose reason is still worth reading (past permanent
	 * deletions, unknown earlier history), so "Healthy" is not read as
	 * "nothing was lost". Shown quietly; never a warning or a notification.
	 */
	note?: boolean;
}

export interface MemoryRepair {
	kind: string;
	count: number;
	at: number;
	summary: string;
}

export interface MemoryHealth {
	state: 'healthy' | 'attention';
	checked_at: number | null;
	signals: MemorySignal[];
	repairs: MemoryRepair[];
}

export function fetchMemoryHealth(): Promise<MemoryHealth> {
	return api.get<MemoryHealth>('/memory/health');
}

function notes(health: MemoryHealth): MemorySignal[] {
	return (health.signals ?? []).filter((s) => s.state === 'healthy' && s.note);
}

/** One-line value for the collapsed row. */
export function memorySummary(health: MemoryHealth | null): string {
	if (!health) return 'Loading';
	if (health.state === 'healthy') {
		const n = notes(health).length;
		return n ? `Healthy, ${n === 1 ? '1 note' : `${n} notes`}` : 'Healthy';
	}
	// No attention signals listed still means attention, never "0 need attention".
	const n = (health.signals ?? []).filter((s) => s.state === 'attention').length;
	return n > 1 ? `${n} need attention` : 'Needs attention';
}

/**
 * When the worker last ran the check, for the row's hint ("Checked 12m ago"),
 * so a fresh warning can be told from an old one. Same wording as the
 * Memory page's times.
 */
export function memoryChecked(health: MemoryHealth, now = Date.now()): string {
	const date = new Date((health.checked_at ?? NaN) * 1000);
	if (!health.checked_at || Number.isNaN(date.getTime())) return 'Not checked yet';
	const ago = relativeAgo(date.toISOString(), now);
	return ago ? `Checked ${ago}` : 'Not checked yet';
}

/**
 * The same moment in full, for the opened list ("Last checked Nov 9, 2026,
 * 8:40 PM"). Empty before the first check: the hint already says so.
 */
export function memoryCheckedAt(health: MemoryHealth): string {
	const date = new Date((health.checked_at ?? NaN) * 1000);
	if (!health.checked_at || Number.isNaN(date.getTime())) return '';
	return `Last checked ${date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`;
}

const WEEK = 7 * 24 * 3600;

function when(at: number): string {
	const date = new Date(at * 1000);
	return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/**
 * The expanded list: the signals that need attention, then healthy signals
 * with a note (a neutral dot, e.g. past archive deletions), then what was
 * repaired this week. A healthy store with nothing else to say shows one line.
 */
export function memoryRows(health: MemoryHealth, now = Date.now() / 1000): UpdateResultRow[] {
	const rows: UpdateResultRow[] = (health.signals ?? [])
		.filter((s) => s.state === 'attention')
		.map((s) => ({ id: `signal-${s.id}`, name: s.label, tone: 'warn', text: s.reason }));
	for (const s of notes(health)) rows.push({ id: `note-${s.id}`, name: s.label, tone: 'off', text: s.reason });
	for (const [i, repair] of (health.repairs ?? []).entries()) {
		if (now - repair.at > WEEK) continue;
		rows.push({ id: `repair-${i}`, name: 'Repaired', tone: 'ok', text: `${repair.summary} ${when(repair.at)}`.trim() });
	}
	if (!rows.length) rows.push({ id: 'ok', name: 'All checks', tone: 'ok', text: 'Nothing needs attention' });
	return rows;
}
