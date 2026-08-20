/**
 * Transcript grouping — turns the flat message array into the display list the
 * transcript renders.
 *
 * The shape follows the T3 work-log pattern:
 *  - While a turn is IN PROGRESS only the newest tool row is on screen; the
 *    earlier ones sit behind a "+N previous tool calls" toggle, so a long tool
 *    run doesn't push the live output off the top of the viewport.
 *  - Once a turn has SETTLED its intermediate tool rows and its non-final
 *    commentary collapse behind one "Worked for {duration}" row; the answer the
 *    user actually wants — the turn's final assistant message — stays visible.
 *
 * Some things are never folded, because hiding them would hide a decision or a
 * failure: subagent cards, todo lists, questions, screenshots, and any tool row
 * that errored.
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

function durationLabel(turn: ChatMessage[]): string | null {
	const ms = turnDurationMs(turn);
	if (ms === null) return null;
	return `${turnWasInterrupted(turn) ? 'Stopped after' : 'Worked for'} ${formatDuration(ms)}`;
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
 * Ordering rule: the fold occupies the slot of the FIRST message it swallowed,
 * and everything it did not swallow keeps its original relative order. So a
 * subagent card that ran between two tool calls stays where it happened, and
 * both tool calls end up in the one fold above it.
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
		const turnMessages = turn.map((t) => t.message);

		// The turn's final assistant text is the answer — never folded.
		let finalTextIndex = -1;
		for (let i = turn.length - 1; i >= 0; i--) {
			if (isAssistantText(turn[i].message)) {
				finalTextIndex = i;
				break;
			}
		}

		const foldable: WorkLogEntry[] = [];
		const rest: Positioned[] = [];
		let foldSlot = -1;

		turn.forEach((entry, i) => {
			let fold = false;
			if (groupingEnabled) {
				if (isFoldableTool(entry.message)) {
					fold = true;
				} else if (!isLiveTurn && isAssistantText(entry.message) && i !== finalTextIndex) {
					// Settled turns also swallow the running commentary between tool
					// calls; while the turn is live that commentary is the only thing
					// telling the user what is happening, so it stays.
					fold = true;
				}
			}
			if (fold) {
				if (foldSlot === -1) foldSlot = rest.length;
				foldable.push({ message: entry.message, index: entry.index });
			} else {
				rest.push(entry);
			}
		});

		// Live turns keep the newest tool row on screen.
		let folded = foldable;
		let tail: WorkLogEntry[] = [];
		if (isLiveTurn && foldable.length > 0) {
			folded = foldable.slice(0, -1);
			tail = foldable.slice(-1);
		}

		if (foldable.length === 0) {
			emitWithSystemGroups(rest, items);
			return;
		}

		if (folded.length === 0) {
			// Nothing to hide — render the visible rows in place, no fold chrome.
			const merged = [...rest];
			merged.splice(foldSlot === -1 ? merged.length : foldSlot, 0, ...tail);
			emitWithSystemGroups(merged, items);
			return;
		}

		const before = rest.slice(0, foldSlot === -1 ? rest.length : foldSlot);
		const after = rest.slice(foldSlot === -1 ? rest.length : foldSlot);

		emitWithSystemGroups(before, items);
		items.push({
			kind: 'work_log',
			key: `wl-${folded[0].message.id ?? folded[0].index}`,
			mode: isLiveTurn ? 'live' : 'settled',
			folded,
			tail,
			durationLabel: isLiveTurn ? null : durationLabel(turnMessages)
		});
		emitWithSystemGroups(after, items);
	});

	return items;
}
