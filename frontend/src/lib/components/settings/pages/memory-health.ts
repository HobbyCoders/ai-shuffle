/**
 * System > Memory > Memory health: the worker's own check of long-term memory
 * (GET /memory/health, app/core/fluid_memory.py owner_health). Read-only: the
 * worker repairs what it safely can by itself; this only reports.
 */
import { api } from '$lib/api/client';
import type { UpdateResultRow } from './updates-state.svelte';

export interface MemorySignal {
	id: string;
	label: string;
	state: 'healthy' | 'attention';
	reason: string;
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

/** One-line value for the collapsed row. */
export function memorySummary(health: MemoryHealth | null): string {
	if (!health) return 'Loading';
	if (health.state === 'healthy') return 'Healthy';
	const n = (health.signals ?? []).filter((s) => s.state === 'attention').length;
	return n === 1 ? 'Needs attention' : `${n} need attention`;
}

const WEEK = 7 * 24 * 3600;

function when(at: number): string {
	const date = new Date(at * 1000);
	return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/**
 * The expanded list: the signals that need attention, then what was repaired
 * this week. A healthy store with nothing repaired shows one line.
 */
export function memoryRows(health: MemoryHealth, now = Date.now() / 1000): UpdateResultRow[] {
	const rows: UpdateResultRow[] = (health.signals ?? [])
		.filter((s) => s.state === 'attention')
		.map((s) => ({ id: `signal-${s.id}`, name: s.label, tone: 'warn', text: s.reason }));
	for (const [i, repair] of (health.repairs ?? []).entries()) {
		if (now - repair.at > WEEK) continue;
		rows.push({ id: `repair-${i}`, name: 'Repaired', tone: 'ok', text: `${repair.summary} ${when(repair.at)}`.trim() });
	}
	if (!rows.length) rows.push({ id: 'ok', name: 'All checks', tone: 'ok', text: 'Nothing needs attention' });
	return rows;
}
