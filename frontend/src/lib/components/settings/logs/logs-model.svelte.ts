/**
 * Logs: the worker's recent log lines, newest first.
 *
 * Level is a server filter; the search box is a server filter on the
 * message, debounced 300ms; "Same logger" narrows the loaded lines on the
 * client. The collection polls every 10s, only while Settings is the visible
 * panel and the browser tab is visible.
 */
import { api, fetchAndDownload } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import type { StatusTone } from '$lib/settings/types';

export interface LogLine {
	timestamp: string;
	level: string;
	logger_name: string;
	message: string;
}

export interface LogsResponse {
	lines: LogLine[];
	total: number;
	returned: number;
	levels: string[];
}

/** A loaded line with a stable id for selection. */
export interface LogEntry extends LogLine {
	id: string;
}

export const LOG_LIMIT = 500;
export const LOG_POLL_MS = 10_000;
export const SEARCH_DEBOUNCE_MS = 300;
export const LEVEL_ORDER = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
export const EXPORT_FILENAME = 'ai-shuffle-logs.log';

export function levelTone(level: string): StatusTone {
	switch (level.toUpperCase()) {
		case 'INFO':
			return 'ok';
		case 'WARNING':
			return 'warn';
		case 'ERROR':
		case 'CRITICAL':
			return 'err';
		default:
			return 'off';
	}
}

/** "WARNING" -> "Warning". */
export function levelName(level: string): string {
	const l = level.toLowerCase();
	return l.charAt(0).toUpperCase() + l.slice(1);
}

function pad(n: number, width = 2): string {
	return String(n).padStart(width, '0');
}

/** HH:MM:SS.mmm in local time; the raw text when unparsable. */
export function formatLogTime(ts: string): string {
	const d = new Date(ts);
	if (isNaN(d.getTime())) return ts;
	return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** Known levels first in severity order, then any others the worker reports. */
export function sortLevels(levels: readonly string[]): string[] {
	return Array.from(new Set([...LEVEL_ORDER, ...levels])).sort((a, b) => {
		const ai = LEVEL_ORDER.indexOf(a);
		const bi = LEVEL_ORDER.indexOf(b);
		if (ai === -1 && bi === -1) return a.localeCompare(b);
		if (ai === -1) return 1;
		if (bi === -1) return -1;
		return ai - bi;
	});
}

function hash(text: string): string {
	let h = 0;
	for (let i = 0; i < text.length; i++) h = (Math.imul(31, h) + text.charCodeAt(i)) | 0;
	return (h >>> 0).toString(36);
}

/**
 * Ids that survive polling: the same line keeps its id while it stays in the
 * window. Identical lines are told apart by how many came before them.
 */
export function withIds(lines: readonly LogLine[]): LogEntry[] {
	const seen = new Map<string, number>();
	return lines.map((line) => {
		const base = `${line.timestamp}|${line.level}|${line.logger_name}|${hash(line.message)}`;
		const n = seen.get(base) ?? 0;
		seen.set(base, n + 1);
		return { ...line, id: n ? `${base}|${n}` : base };
	});
}

export class LogsModel {
	/** Newest first. */
	entries = $state<LogEntry[]>([]);
	total = $state(0);
	returned = $state(0);
	levels = $state<string[]>([]);
	loading = $state(true);
	loaded = $state(false);
	forbidden = $state(false);
	error = $state('');
	exporting = $state(false);
	level = $state('');
	/** What the user typed. */
	search = $state('');
	/** What the last fetch sent (after the debounce). */
	appliedSearch = $state('');
	/** Client-side "Same logger" narrowing. */
	logger = $state('');
	/** Last-seen copy of the selected line, kept after it scrolls out. */
	pinned = $state<LogEntry | null>(null);

	#debounce: ReturnType<typeof setTimeout> | undefined;
	#seq = 0;

	get shown(): LogEntry[] {
		return this.logger ? this.entries.filter((e) => e.logger_name === this.logger) : this.entries;
	}

	get levelOptions(): string[] {
		return sortLevels(this.levels);
	}

	get errorCount(): number {
		return this.entries.filter((e) => levelTone(e.level) === 'err').length;
	}

	find(id: string | null | undefined): LogEntry | null {
		if (!id) return null;
		const hit = this.entries.find((e) => e.id === id);
		if (hit) return hit;
		return this.pinned?.id === id ? this.pinned : null;
	}

	pin(id: string): void {
		this.pinned = this.entries.find((e) => e.id === id) ?? this.pinned;
	}

	async fetch(): Promise<void> {
		const seq = ++this.#seq;
		try {
			const params = new URLSearchParams({ limit: String(LOG_LIMIT) });
			if (this.level) params.set('level', this.level);
			if (this.appliedSearch) params.set('search', this.appliedSearch);
			const data = await api.get<LogsResponse>(`/logs?${params.toString()}`);
			if (seq !== this.#seq) return;
			// The server sends newest first already.
			this.entries = withIds(data.lines);
			this.total = data.total;
			this.returned = data.returned;
			if (data.levels.length > 0) this.levels = data.levels;
			this.error = '';
			this.forbidden = false;
			this.loaded = true;
		} catch (err) {
			if (seq !== this.#seq) return;
			if (isForbidden(err)) this.forbidden = true;
			else this.error = (err as { detail?: string })?.detail || 'Failed to load logs';
		} finally {
			if (seq === this.#seq) this.loading = false;
		}
	}

	refresh(): Promise<void> {
		this.loading = true;
		return this.fetch();
	}

	setLevel(level: string): void {
		this.level = level;
	}

	/** Debounced: the fetch follows 300ms after the last keystroke. */
	setSearch(text: string): void {
		this.search = text;
		clearTimeout(this.#debounce);
		this.#debounce = setTimeout(() => {
			this.appliedSearch = text.trim();
		}, SEARCH_DEBOUNCE_MS);
	}

	sameLogger(logger: string): void {
		this.logger = logger;
	}

	clearLogger(): void {
		this.logger = '';
	}

	async exportLogs(): Promise<void> {
		this.exporting = true;
		try {
			await fetchAndDownload('/logs/export', EXPORT_FILENAME);
		} catch (err) {
			this.error = (err as { detail?: string })?.detail || 'Export failed';
		} finally {
			this.exporting = false;
		}
	}

	dispose(): void {
		clearTimeout(this.#debounce);
		this.#seq++;
	}

	/** Back to a fresh state (tests). */
	reset(): void {
		this.dispose();
		this.entries = [];
		this.total = 0;
		this.returned = 0;
		this.levels = [];
		this.loading = true;
		this.loaded = false;
		this.forbidden = false;
		this.error = '';
		this.exporting = false;
		this.level = '';
		this.search = '';
		this.appliedSearch = '';
		this.logger = '';
		this.pinned = null;
	}
}

/** One model per app so the index can show the error count Logs last saw. */
export const logsModel = new LogsModel();
