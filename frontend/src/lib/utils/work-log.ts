/**
 * Transcript presentation model.
 *
 * AI Shuffle keeps the wire/store model flat and derives the compact transcript
 * here. The rules deliberately mirror T3 Code's readable turn rhythm without
 * adopting its data model:
 *
 * - assistant prose is never withheld while a turn is live;
 * - a contiguous live tool run keeps its newest active call visible;
 * - once a turn settles, non-final commentary, reasoning, and routine tools fold
 *   behind one compact work row while the final answer remains visible;
 * - failures, screenshots, plans/questions, and agent surfaces never disappear
 *   behind a fold.
 */
import type { ChatMessage } from '$lib/stores/types';
import { displayToolName } from './tool-icons';

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
			/** "Worked for 2m 14s" / "You stopped after 12s". */
			durationLabel: string | null;
			/** Provider-neutral description such as "Read 3 files and ran 2 commands". */
			summary: string | null;
	  };

/** Low-signal system messages that may collapse when consecutive. */
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

/** Dedicated/decision-bearing tools retain their own presentation. */
function isSpecialTool(msg: ChatMessage): boolean {
	return (
		msg.toolName === 'TodoWrite' ||
		msg.toolName === 'Note' ||
		msg.toolName === 'Task' ||
		msg.toolName === 'Agent' ||
		msg.toolName === 'AskUserQuestion'
	);
}

function isRoutineTool(msg: ChatMessage): boolean {
	if (msg.type !== 'tool_use' || isSpecialTool(msg)) return false;
	// A failure must remain visible, as must a screenshot (the screenshot is the
	// useful result rather than incidental detail).
	return msg.toolStatus !== 'error' && !msg.toolScreenshot;
}

function isAssistantText(msg: ChatMessage): boolean {
	return msg.role === 'assistant' && (msg.type === 'text' || !msg.type) && !!msg.content?.trim();
}

function isAssistantPhantom(msg: ChatMessage): boolean {
	return msg.role === 'assistant' && (msg.type === 'text' || !msg.type) && !msg.content?.trim();
}

/** Only work known to be closed may disappear in a settled disclosure. */
function isCompletedRoutineWork(msg: ChatMessage): boolean {
	if (msg.type === 'thinking') return !msg.streaming;
	return isRoutineTool(msg) && msg.toolStatus === 'complete' && !msg.streaming;
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
	const timestamp = Date.parse(value);
	return Number.isFinite(timestamp) ? timestamp : null;
}

/** Elapsed wall time across a turn's first and last usable timestamp. */
export function turnDurationMs(turn: ChatMessage[]): number | null {
	const reportedDuration = turn
		.toReversed()
		.map((message) => message.metadata?.duration_ms)
		.find((value): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0);
	if (reportedDuration !== undefined) return reportedDuration;

	let first: number | null = null;
	let last: number | null = null;
	for (const message of turn) {
		const timestamp = parseStamp(message.timestamp);
		if (timestamp === null) continue;
		if (first === null || timestamp < first) first = timestamp;
		if (last === null || timestamp > last) last = timestamp;
	}
	if (first === null || last === null || last <= first) return null;
	return last - first;
}

/** Interrupt marker that survives transcript reloads. */
export function turnWasInterrupted(turn: ChatMessage[]): boolean {
	return turn.some((message) =>
		isAssistantText(message) && message.content.trimEnd().endsWith('[Stopped]')
	);
}

function turnFoldLabel(turn: ChatMessage[], interrupted: boolean): string {
	const duration = turnDurationMs(turn);
	if (interrupted) {
		return duration === null ? 'You stopped this response' : `You stopped after ${formatDuration(duration)}`;
	}
	return duration === null ? 'Worked' : `Worked for ${formatDuration(duration)}`;
}

type ToolAction = 'read' | 'edit' | 'command' | 'code-search' | 'web-search' | 'other';

function toolAction(message: ChatMessage): ToolAction {
	const name = displayToolName(message.toolName || '');
	if (['Read', 'BashOutput', 'TaskOutput', 'TaskGet'].includes(name)) return 'read';
	if (['Write', 'Edit', 'NotebookEdit'].includes(name)) return 'edit';
	if (['Bash', 'KillShell', 'SlashCommand', 'Monitor', 'TaskStop'].includes(name)) return 'command';
	if (['Grep', 'Glob', 'ToolSearch', 'LSP'].includes(name)) return 'code-search';
	if (['WebSearch', 'WebFetch'].includes(name) || /(?:^|_)search$/i.test(name)) return 'web-search';
	return 'other';
}

function actionLabel(action: ToolAction, count: number): string {
	switch (action) {
		case 'read':
			return `Read ${count} ${count === 1 ? 'file' : 'files'}`;
		case 'edit':
			return `Changed ${count} ${count === 1 ? 'file' : 'files'}`;
		case 'command':
			return `Ran ${count} ${count === 1 ? 'command' : 'commands'}`;
		case 'code-search':
			return `Searched code ${count} ${count === 1 ? 'time' : 'times'}`;
		case 'web-search':
			return `Searched the web ${count} ${count === 1 ? 'time' : 'times'}`;
		case 'other':
			return `Used ${count} ${count === 1 ? 'tool' : 'tools'}`;
	}
}

function sentenceJoin(labels: string[]): string {
	const normalized = labels.map((label, index) =>
		index === 0 ? label : label.charAt(0).toLowerCase() + label.slice(1)
	);
	if (normalized.length < 2) return normalized[0] ?? '';
	if (normalized.length === 2) return normalized.join(' and ');
	return `${normalized.slice(0, -1).join(', ')}, and ${normalized.at(-1)}`;
}

/** Provider-neutral, action-specific summary for a folded tool run. */
export function summarizeWork(entries: WorkLogEntry[]): string | null {
	const tools = entries.filter((entry) => entry.message.type === 'tool_use');
	if (tools.length === 0) {
		if (entries.some((entry) => entry.message.type === 'thinking')) return 'Reasoning and interim work';
		return entries.length > 0 ? 'Interim work' : null;
	}

	const counts = new Map<ToolAction, number>();
	for (const entry of tools) {
		const action = toolAction(entry.message);
		counts.set(action, (counts.get(action) ?? 0) + 1);
	}
	return sentenceJoin([...counts].map(([action, count]) => actionLabel(action, count))) || null;
}

interface Positioned {
	message: ChatMessage;
	index: number;
}

/** Split a deduplicated list into turns, each beginning at a user message. */
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

/** Fold consecutive low-signal system messages without disturbing order. */
function emitWithSystemGroups(entries: Positioned[], out: DisplayItem[]) {
	let index = 0;
	while (index < entries.length) {
		const entry = entries[index];
		if (isGroupableSystem(entry.message)) {
			const run: Positioned[] = [entry];
			let cursor = index + 1;
			while (cursor < entries.length && isGroupableSystem(entries[cursor].message)) {
				run.push(entries[cursor]);
				cursor++;
			}
			if (run.length >= 2) {
				out.push({
					kind: 'system_group',
					messages: run.map((item) => item.message),
					indices: run.map((item) => item.index)
				});
			} else {
				out.push({ kind: 'message', message: entry.message, index: entry.index });
			}
			index = cursor;
		} else {
			out.push({ kind: 'message', message: entry.message, index: entry.index });
			index++;
		}
	}
}

export interface BuildOptions {
	/** The session is mid-turn — its last turn renders in live mode. */
	isStreaming: boolean;
	/** User preference: false renders work rows flat, with no folds. */
	groupingEnabled: boolean;
}

function countFoldRuns(
	turn: Positioned[],
	shouldFold: (entry: Positioned) => boolean
): number {
	let count = 0;
	let inRun = false;
	for (const entry of turn) {
		if (shouldFold(entry)) {
			if (!inRun) count++;
			inRun = true;
		} else {
			inRun = false;
		}
	}
	return count;
}

/** Build the transcript display list without changing store or protocol semantics. */
export function buildDisplayItems(messages: ChatMessage[], options: BuildOptions): DisplayItem[] {
	const { isStreaming, groupingEnabled } = options;

	// Duplicate ids crash Svelte's keyed each; drop repeats defensively.
	const seen = new Set<string>();
	const positioned: Positioned[] = [];
	messages.forEach((message, index) => {
		// Empty assistant placeholders intentionally have no visual row. Keeping them
		// in this model can split the final live tool run and hide its active tail.
		if (isAssistantPhantom(message)) return;
		if (message.id) {
			if (seen.has(message.id)) return;
			seen.add(message.id);
		}
		positioned.push({ message, index });
	});

	const items: DisplayItem[] = [];
	const turns = splitTurns(positioned);

	turns.forEach((turn, turnIndex) => {
		const liveTurn = isStreaming && turnIndex === turns.length - 1;
		const interrupted = !liveTurn && turnWasInterrupted(turn.map((entry) => entry.message));
		const terminalAssistant = liveTurn
			? null
			: (turn.findLast((entry) => isAssistantText(entry.message)) ?? null);
		// A live group may include exactly the newest active routine tool as its
		// visible tail. Any anomalous earlier running tools remain flat and visible.
		const newestRunningTool = liveTurn
			? (turn.findLast(
					(entry) => isRoutineTool(entry.message) && entry.message.toolStatus === 'running'
				) ?? null)
			: null;
		const shouldFold = (entry: Positioned) => {
			if (!groupingEnabled) return false;
			if (liveTurn) {
				return isCompletedRoutineWork(entry.message) || entry === newestRunningTool;
			}
			return (
				isCompletedRoutineWork(entry.message) ||
				(isAssistantText(entry.message) && !entry.message.streaming && entry !== terminalAssistant)
			);
		};
		const foldRunCount = countFoldRuns(turn, shouldFold);
		const wholeTurnLabel = turnFoldLabel(turn.map((entry) => entry.message), interrupted);

		let plain: Positioned[] = [];
		let run: WorkLogEntry[] = [];

		const flushPlain = () => {
			if (plain.length === 0) return;
			emitWithSystemGroups(plain, items);
			plain = [];
		};

		const flushRun = (isTurnEnd: boolean) => {
			if (run.length === 0) return;
			const liveRun = liveTurn && isTurnEnd;

			// A single live row is already compact and must stay visible. Settled
			// single-row activity still folds, matching the finished-turn treatment.
			if (liveRun && run.length === 1) {
				plain.push({ message: run[0].message, index: run[0].index });
				run = [];
				return;
			}

			flushPlain();
			const summary = summarizeWork(liveRun ? run.slice(0, -1) : run);
			if (liveRun) {
				items.push({
					kind: 'work_log',
					key: `wl-${run[0].message.id ?? run[0].index}`,
					mode: 'live',
					folded: run.slice(0, -1),
					tail: run.slice(-1),
					durationLabel: null,
					summary
				});
			} else {
				items.push({
					kind: 'work_log',
					key: `wl-${run[0].message.id ?? run[0].index}`,
					mode: 'settled',
					folded: run,
					tail: [],
					durationLabel: !liveTurn && foldRunCount === 1 ? wholeTurnLabel : summary,
					summary: !liveTurn && foldRunCount === 1 ? summary : null
				});
			}
			run = [];
		};

		turn.forEach((entry) => {
			if (shouldFold(entry)) {
				run.push({ message: entry.message, index: entry.index });
				return;
			}

			flushRun(false);
			plain.push(entry);
		});
		flushRun(true);
		flushPlain();
	});

	return items;
}
