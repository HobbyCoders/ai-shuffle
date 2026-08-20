/**
 * Transcript grouping — turns the flat message array into the display list the
 * transcript renders.
 *
 * The shape follows the T3 work-log pattern, folded PER RUN:
 *  - A fold spans one run of consecutive work entries (tool calls + thinking)
 *    between assistant text messages. Assistant text is never folded and always
 *    ends the current run, so the transcript reads text → fold → text → fold.
 *  - While a turn is IN PROGRESS its current run keeps the newest row on
 *    screen; the earlier ones sit behind a "+N previous tool calls" toggle, so
 *    a long tool run doesn't push the live output off the top of the viewport.
 *  - A SETTLED run collapses behind one "Worked for {duration}" row ("{N} tool
 *    calls" when no timestamps are available). A single-row run renders flat —
 *    fold chrome around one row hides nothing.
 *
 * Some things are never folded, because hiding them would hide a decision or a
 * failure: subagent cards, todo/notes lists, questions, screenshots, and any
 * tool row that errored.
 */
import type { ChatMessage } from '$lib/stores/types';

export interface WorkLogEntry {
	message: ChatMessage;
	index: number;
}

export type DisplayItem =
	| { kind: 'message'; message: ChatMessage; index: number }
	| { kind: 'system_group'; messages: ChatMessage[]; indices: number[] }
	| {
			kind: 'work_log';
			key: string;
			/** 'live' = the turn is still running; 'settled' = it finished. */
			mode: 'live' | 'settled';
			/** Rows hidden behind the fold. */
			folded: WorkLogEntry[];
			/** Rows always visible below the fold (live mode's newest row). */
			tail: WorkLogEntry[];
			/** "Worked for 2m 14s" / "Stopped after 12s", or null when unknown. */
			durationLabel: string | null;
	  };

/**
 * Low-signal system subtypes that collapse into a single summary row when they
 * arrive back-to-back. Errors, compact boundaries, mode changes and rewind
 * notices are deliberately absent — those stay full cards.
 */
const GROUPABLE_SYSTEM_SUBTYPES = new Set([
	'hook_started',
	'hook_response',
	'stop_hook_summary',
	'turn_duration',
	'task_started',
	'task_progress',
	'task_updated',
	'task_notification'
]);

export function isGroupableSystem(msg: ChatMessage): boolean {
	return msg.type === 'system' && GROUPABLE_SYSTEM_SUBTYPES.has(msg.systemSubtype || '');
}

/** Tool rows that get their own dedicated presentation, so never a work-log row. */
function isSpecialTool(msg: ChatMessage): boolean {
	return (
		msg.toolName === 'TodoWrite' ||
		msg.toolName === 'Note' ||
		msg.toolName === 'Task' ||
		msg.toolName === 'Agent' ||
		msg.toolName === 'AskUserQuestion'
	);
}

/** A tool row that may be hidden behind a fold. */
function isFoldableTool(msg: ChatMessage): boolean {
	if (msg.type !== 'tool_use') return false;
	if (isSpecialTool(msg)) return false;
	// A failure must stay on screen; so must a screenshot, which is the whole
	// point of the call that produced it.
	if (msg.toolStatus === 'error') return false;
	if (msg.toolScreenshot) return false;
	return true;
}

function isAssistantText(msg: ChatMessage): boolean {
	return msg.role === 'assistant' && (msg.type === 'text' || !msg.type) && !!msg.content?.trim();
}

/** A work entry a fold may swallow: a routine tool row or a thinking block. */
function isFoldableWork(msg: ChatMessage): boolean {
	if (msg.type === 'thinking') return true;
	return isFoldableTool(msg);
}

/** Human duration: "45s", "2m 14s", "1h 03m". */
export function formatDuration(ms: number): string {
	const total = Math.max(0, Math.round(ms / 1000));
	if (total < 60) return `${total}s`;
	const mins = Math.floor(total / 60);
	const secs = total % 60;
	if (mins < 60) return secs ? `${mins}m ${secs}s` : `${mins}m`;
	const hours = Math.floor(mins / 60);
	const remMins = mins % 60;
	return remMins ? `${hours}h ${String(remMins).padStart(2, '0')}m` : `${hours}h`;
}

function parseStamp(value: string | undefined): number | null {
	if (!value) return null;
	const t = Date.parse(value);
	return Number.isFinite(t) ? t : null;
}

/**
 * Elapsed wall time across a turn, from its first to its last stamped message.
 * Returns null when fewer than two messages carry a usable timestamp — the
 * label then degrades to a plain "Show work log" rather than a made-up number.
 */
export function turnDurationMs(turn: ChatMessage[]): number | null {
	let first: number | null = null;
	let last: number | null = null;
	for (const m of turn) {
		const t = parseStamp(m.timestamp);
		if (t === null) continue;
		if (first === null || t < first) first = t;
		if (last === null || t > last) last = t;
	}
	if (first === null || last === null || last <= first) return null;
	return last - first;
}

/**
 * True when the turn was cut short by the user. The reducer stamps "[Stopped]"
 * onto the truncated assistant text on `turn.aborted(reason='interrupt')`, and
 * that marker is the only interrupt signal that survives a reload.
 */
export function turnWasInterrupted(turn: ChatMessage[]): boolean {
	return turn.some((m) => isAssistantText(m) && m.content.trimEnd().endsWith('[Stopped]'));
}

/**
 * Label for one settled run. Timestamps give "Worked for 2m 14s" (or "Stopped
 * after" on an interrupted turn's final run); without them the label degrades
 * to a plain count.
 */
function runLabel(run: WorkLogEntry[], interrupted: boolean): string {
	const msgs = run.map((r) => r.message);
	const ms = turnDurationMs(msgs);
	if (ms === null) {
		const tools = msgs.filter((m) => m.type === 'tool_use').length;
		const n = tools || run.length;
		return `${n} tool ${n === 1 ? 'call' : 'calls'}`;
	}
	return `${interrupted ? 'Stopped after' : 'Worked for'} ${formatDuration(ms)}`;
}

interface Positioned {
	message: ChatMessage;
	index: number;
}

/** Split the (deduplicated) message list into turns, each starting at a user message. */
function splitTurns(messages: Positioned[]): Positioned[][] {
	const turns: Positioned[][] = [];
	let current: Positioned[] = [];
	for (const entry of messages) {
		if (entry.message.role === 'user' && current.length > 0) {
			turns.push(current);
			current = [];
		}
		current.push(entry);
	}
	if (current.length > 0) turns.push(current);
	return turns;
}

/** Fold consecutive low-signal system messages inside an already-ordered run. */
function emitWithSystemGroups(entries: Positioned[], out: DisplayItem[]) {
	let i = 0;
	while (i < entries.length) {
		const entry = entries[i];
		if (isGroupableSystem(entry.message)) {
			const run: Positioned[] = [entry];
			let j = i + 1;
			while (j < entries.length && isGroupableSystem(entries[j].message)) {
				run.push(entries[j]);
				j++;
			}
			if (run.length >= 2) {
				out.push({
					kind: 'system_group',
					messages: run.map((r) => r.message),
					indices: run.map((r) => r.index)
				});
			} else {
				out.push({ kind: 'message', message: entry.message, index: entry.index });
			}
			i = j;
		} else {
			out.push({ kind: 'message', message: entry.message, index: entry.index });
			i++;
		}
	}
}

export interface BuildOptions {
	/** The session is mid-turn — its LAST turn renders in live mode. */
	isStreaming: boolean;
	/** User preference: false renders every tool row flat, with no folds. */
	groupingEnabled: boolean;
}

/**
 * Build the transcript's display list.
 *
 * Ordering rule: everything is emitted in transcript order. A fold sits exactly
 * where its run of work entries happened; assistant text, subagent cards, and
 * every other non-work row stay in place and end the run they interrupt.
 */
export function buildDisplayItems(messages: ChatMessage[], options: BuildOptions): DisplayItem[] {
	const { isStreaming, groupingEnabled } = options;

	// Duplicate ids crash Svelte's keyed {#each}; they can arrive from delta-merge
	// edge cases and corrupted caches, so drop repeats before anything else.
	const seen = new Set<string>();
	const positioned: Positioned[] = [];
	messages.forEach((message, index) => {
		if (message.id) {
			if (seen.has(message.id)) return;
			seen.add(message.id);
		}
		positioned.push({ message, index });
	});

	const items: DisplayItem[] = [];
	const turns = splitTurns(positioned);

	turns.forEach((turn, turnIdx) => {
		const isLiveTurn = isStreaming && turnIdx === turns.length - 1;
		const interrupted = !isLiveTurn && turnWasInterrupted(turn.map((t) => t.message));

		// Plain rows waiting to be emitted, and the current run of work entries.
		let plain: Positioned[] = [];
		let run: WorkLogEntry[] = [];

		const flushPlain = () => {
			if (plain.length === 0) return;
			emitWithSystemGroups(plain, items);
			plain = [];
		};

		// `isTurnEnd` marks the flush at the end of the turn: on a live turn that
		// run is the one still growing, and on an interrupted turn it is the run
		// that was cut short.
		const flushRun = (isTurnEnd: boolean) => {
			if (run.length === 0) return;
			const live = isLiveTurn && isTurnEnd;

			// Fold chrome around a single row hides nothing — render it flat.
			if (run.length === 1) {
				plain.push({ message: run[0].message, index: run[0].index });
				run = [];
				return;
			}

			flushPlain();
			if (live) {
				items.push({
					kind: 'work_log',
					key: `wl-${run[0].message.id ?? run[0].index}`,
					mode: 'live',
					folded: run.slice(0, -1),
					tail: run.slice(-1),
					durationLabel: null
				});
			} else {
				items.push({
					kind: 'work_log',
					key: `wl-${run[0].message.id ?? run[0].index}`,
					mode: 'settled',
					folded: run,
					tail: [],
					durationLabel: runLabel(run, interrupted && isTurnEnd)
				});
			}
			run = [];
		};

		turn.forEach((entry) => {
			if (groupingEnabled && isFoldableWork(entry.message)) {
				run.push({ message: entry.message, index: entry.index });
			} else {
				flushRun(false);
				plain.push(entry);
			}
		});
		flushRun(true);
		flushPlain();
	});

	return items;
}
