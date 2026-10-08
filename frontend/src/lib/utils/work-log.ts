/**
 * Transcript presentation model.
 *
 * AI Shuffle keeps the wire/store model flat and derives the compact transcript
 * here. The rules deliberately mirror T3 Code's readable turn rhythm without
 * adopting its data model:
 *
 * - assistant prose is never withheld while a turn is live;
 * - every live work row stays visible until the turn settles;
 * - once a turn settles, non-final commentary, reasoning, and routine tools fold
 *   behind one compact work row while the final answer remains visible;
 * - failed tool calls fold with the rest of the run and are counted in the work
 *   summary, while screenshots, plans/questions, and still-running agent surfaces
 *   never disappear behind a fold.
 */
import type { ChatMessage } from '$lib/stores/types';
import { galleryMediaForTurn, type GalleryMedia } from './media-gallery';
import { agentDelivery } from './agent-delivery';
import { canRewindUserMessage } from './message-presentation';
import { isNavigableUserTurn } from './transcript-navigation';
import { isTransientProviderRetry } from '$lib/utils/provider-retry';
import { displayToolName } from './tool-icons';
import { isSwarmTool, parseSwarm, resolveSwarm, type SwarmSnapshot } from './swarm';

export interface WorkLogEntry {
	message: ChatMessage;
	index: number;
}

/**
 * A settled turn keeps one ordered sequence inside its disclosure. Protected
 * rows and the terminal assistant response have `hiddenWhenCollapsed=false`,
 * so they remain visible without fragmenting the turn into several folds.
 */
export interface WorkLogSequenceEntry extends WorkLogEntry {
	hiddenWhenCollapsed: boolean;
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
			/** Full settled-turn order, including protected rows and terminal prose. */
			sequence: WorkLogSequenceEntry[];
			gallery?: GalleryMedia[];
			/** "Worked for 2m 14s" / "You stopped after 12s". */
			durationLabel: string | null;
			/** Provider-neutral description such as "Read 3 files and ran 2 commands". */
			summary: string | null;
			/** Token-condensed variant ("Read 3 · Ran 2") for narrow screens. */
			compactSummary: string | null;
	  };

/**
 * Everything the transcript view needs, derived together from `messages`.
 *
 * The view used to compute two of these per RENDERED ROW inside the template:
 * `userPromptOccurrence()` scanned every preceding message for each user row,
 * and the Task/Agent branch ran `messages.some(...)` for each delegation row.
 * With M rows mounted that is O(N·M) — re-run on every streaming fold, which is
 * why streaming got choppier as a session grew (see PERFORMANCE_REVIEW §3.2 R3).
 * Both are now accumulated by the walk `buildDisplayItems` already performs, so
 * the per-row lookup is a single array/Set read.
 */
export interface TranscriptModel {
	/** The grouped presentation list — identical to `buildDisplayItems()`. */
	items: DisplayItem[];
	/**
	 * Occurrence ordinal of each user PROMPT among earlier prompts with the same
	 * trimmed content, indexed by MESSAGE index (0 for every other message).
	 * Indexed by message index — not item index — because the same lookup serves
	 * rows rendered inline and rows rendered inside a work-log disclosure, which
	 * both carry their original message index.
	 */
	promptOccurrence: number[];
	/** Whether each raw message has an independently resolvable user checkpoint. */
	rewindEligible: boolean[];
	/**
	 * Tool ids that own a linked `subagent` card, so a Task/Agent `tool_use`
	 * knows whether the richer card already renders its work. Use
	 * `hasLinkedSubagent()` rather than reading this directly — it encodes the
	 * id-less case the old `some()` scan matched.
	 */
	subagentToolIds: ReadonlySet<string>;
	/** Display-item index of each navigable user turn, by message id. */
	itemIndexByTurnId: ReadonlyMap<string, number>;
	/**
	 * Metadata of the LAST assistant message that carries any (cost/duration are
	 * rendered from it). Also a single-pass result: the view used to run a
	 * separate `messages.findLast(...)` over the whole array on every fold.
	 */
	lastAssistantMetadata: Record<string, unknown> | null;
	/** Id of the last navigable user turn — the transcript's live anchor. */
	latestUserTurnId: string | null;
}

/**
 * Sentinel for subagent cards that carry no `toolId`. The scan this replaced
 * (`messages.some(m => m.type === 'subagent' && m.toolId === message.toolId)`)
 * matched an id-less Task against an id-less subagent card, so the set has to
 * be able to represent "no id" to stay behaviour-identical.
 */
const NO_TOOL_ID = '\u0000no-tool-id';

/** True when a Task/Agent `tool_use` already has a SubagentMessage rendering it. */
export function hasLinkedSubagent(
	model: Pick<TranscriptModel, 'subagentToolIds'>,
	toolId: string | undefined | null
): boolean {
	return model.subagentToolIds.has(toolId ?? NO_TOOL_ID);
}

/**
 * Reference implementation of the per-row prompt-occurrence scan, kept as the
 * oracle the model is tested against. Not used at render time — O(N) per row.
 */
export function userPromptOccurrenceAt(messages: ChatMessage[], index: number): number {
	const want = (messages[index]?.content ?? '').trim();
	let occurrence = 0;
	for (let k = 0; k < index; k++) {
		const m = messages[k];
		if (m.role === 'user' && m.type !== 'command' && (m.content ?? '').trim() === want) {
			occurrence++;
		}
	}
	return occurrence;
}

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

function isGroupableSystem(msg: ChatMessage): boolean {
	return msg.type === 'system' && GROUPABLE_SYSTEM_SUBTYPES.has(msg.systemSubtype || '');
}

/**
 * "Context compacted" cards. One that lands in the middle of a turn is part of
 * that turn's work: it folds into the Worked group and stays a row in the
 * expanded log. The replay's elided-prefix marker (`compaction_boundary`) and
 * a failed compaction are not work and keep their own row.
 */
const COMPACTION_CARD_SUBTYPES = new Set(['compact_boundary', 'microcompact_boundary']);

function isCompactionCard(msg: ChatMessage): boolean {
	return (msg.type === 'system' || msg.role === 'system')
		&& COMPACTION_CARD_SUBTYPES.has(msg.systemSubtype || '');
}

/** Work the agent did after a compaction proves the compaction was mid-turn. */
function isTurnWork(msg: ChatMessage): boolean {
	return msg.role === 'assistant' || msg.type === 'tool_use' || msg.type === 'subagent';
}

/** Dedicated/decision-bearing tools retain their own presentation. */
function isSpecialTool(msg: ChatMessage): boolean {
	return (
		isSwarmTool(msg.toolName ?? '') ||
		msg.toolName === 'TodoWrite' ||
		msg.toolName === 'Task' ||
		msg.toolName === 'Agent' ||
		msg.toolName === 'AskUserQuestion' ||
		msg.toolName === 'AskUser'
	);
}

function isRoutineTool(msg: ChatMessage, gallery = false): boolean {
	if (msg.type !== 'tool_use' || isSpecialTool(msg)) return false;
	// Keep screenshots visible unless a gallery represents them outside the fold.
	// Failures still fold with the run and are counted in its summary.
	return gallery || !msg.toolScreenshot;
}

/**
 * A subagent card whose `agentStatus` has settled ('completed'/'error') is a
 * finished delegation: routine work that belongs inside the turn's fold. A
 * 'pending'/'running' card is live and keeps its own row, and so does a
 * 'stopped' one: somebody stopped it on purpose, and folding it away made the
 * card look like it went missing (#1806).
 */
function isSettledSubagent(msg: ChatMessage): boolean {
	return msg.type === 'subagent' && (msg.agentStatus === 'completed' || msg.agentStatus === 'error');
}

/** A terminal retry failure folds only after recovery or supersession. */
function isFoldableRetryCard(msg: ChatMessage): boolean {
	if (msg.systemSubtype !== 'provider_retry') return false;
	if (msg.type !== 'system' && msg.role !== 'system') return false;
	const data = msg.systemData as Record<string, unknown> | undefined;
	if (!data) return false;
	return data.exhausted === true && (data.recovered === true || data.superseded === true);
}

function isAssistantText(msg: ChatMessage): boolean {
	return msg.role === 'assistant' && (msg.type === 'text' || !msg.type) && !!msg.content?.trim();
}

function isAssistantPhantom(msg: ChatMessage): boolean {
	return msg.role === 'assistant' && (msg.type === 'text' || !msg.type) && !msg.content?.trim();
}

/** Only work known to be closed may disappear in a settled disclosure. */
function isCompletedRoutineWork(msg: ChatMessage, gallery = false): boolean {
	if (agentDelivery(msg)) return true;
	if (msg.type === 'thinking') return !msg.streaming;
	if (msg.type === 'subagent') return isSettledSubagent(msg);
	// A finished retry note is routine work: it folds with the run instead of
	// stacking a dozen standalone cards down a turn that recovered on its own.
	if (isFoldableRetryCard(msg)) return true;
	// A hidden code-mode cell draws nothing at all. Folding it keeps a live run
	// contiguous instead of splitting the chip around an invisible row.
	if (isHiddenCodeModeCell(msg)) return true;
	// Persisted/imported history has used both spellings. Normalize at the
	// presentation boundary rather than widening transport semantics.
	const status = msg.toolStatus as string | undefined;
	const settled = status === 'complete' || status === 'completed' || status === 'error';
	return isRoutineTool(msg, gallery) && settled && !msg.streaming;
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


type ToolAction =
	| 'read'
	| 'edit'
	| 'note'
	| 'command'
	| 'code-search'
	| 'web-search'
	| 'delegate'
	| 'image-gen'
	| 'image-edit'
	| 'video-gen'
	| 'audio-gen'
	| 'threed-gen'
	| 'other';

/** Media-generation tool families, matched on the MCP-stripped bare name. */
const IMAGE_GEN_TOOLS = ['generateImage', 'generateWithReference'];
const IMAGE_EDIT_TOOLS = ['editImage'];
const VIDEO_GEN_TOOLS = ['generateVideo', 'imageToVideo', 'extendVideo'];
const AUDIO_GEN_TOOLS = ['generateAudio', 'audioToAudio'];
const THREED_GEN_TOOLS = ['textTo3D', 'imageTo3D', 'retexture3D', 'rig3D', 'animate3D'];

function toolAction(message: ChatMessage): ToolAction {
	// A subagent card is work handed to someone else, not a tool call of its own.
	if (message.type === 'subagent') return 'delegate';
	const name = displayToolName(message.toolName || '');
	if (IMAGE_GEN_TOOLS.includes(name)) return 'image-gen';
	if (IMAGE_EDIT_TOOLS.includes(name)) return 'image-edit';
	if (VIDEO_GEN_TOOLS.includes(name)) return 'video-gen';
	if (AUDIO_GEN_TOOLS.includes(name)) return 'audio-gen';
	if (THREED_GEN_TOOLS.includes(name)) return 'threed-gen';
	if (name === 'Note') return 'note';
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
		case 'note':
			return `Took ${count} ${count === 1 ? 'note' : 'notes'}`;
		case 'command':
			return `Ran ${count} ${count === 1 ? 'command' : 'commands'}`;
		case 'code-search':
			return `Searched code ${count} ${count === 1 ? 'time' : 'times'}`;
		case 'web-search':
			return `Searched the web ${count} ${count === 1 ? 'time' : 'times'}`;
		case 'delegate':
			return `Delegated ${count} ${count === 1 ? 'task' : 'tasks'}`;
		case 'image-gen':
			return `Generated ${count} ${count === 1 ? 'image' : 'images'}`;
		case 'image-edit':
			return `Edited ${count} ${count === 1 ? 'image' : 'images'}`;
		case 'video-gen':
			return `Generated ${count} ${count === 1 ? 'video' : 'videos'}`;
		case 'audio-gen':
			return `Generated ${count} ${count === 1 ? 'audio track' : 'audio tracks'}`;
		case 'threed-gen':
			return `Built ${count} ${count === 1 ? '3D model' : '3D models'}`;
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

/** Narrow-screen condensation of an action label: "Read 3 files" -> "Read 3". */
const COMPACT_ACTION_LABELS: Record<ToolAction, (count: number) => string> = {
	read: (n) => `Read ${n}`,
	edit: (n) => `Edit ${n}`,
	note: (n) => `Notes ${n}`,
	command: (n) => `Ran ${n}`,
	'code-search': (n) => `Search ${n}`,
	'web-search': (n) => `Web ${n}`,
	delegate: (n) => `Delegated ${n}`,
	'image-gen': (n) => `Images ${n}`,
	'image-edit': (n) => `ImgEdits ${n}`,
	'video-gen': (n) => `Videos ${n}`,
	'audio-gen': (n) => `Audio ${n}`,
	'threed-gen': (n) => `3D ${n}`,
	other: (n) => `Tools ${n}`
};

/**
 * The same tally as `summarizeWork`, condensed to fit a phone's fold row:
 * "Read 2 · Edit 1 · Ran 4 · Search 3 · 1 failed". Every token still carries
 * its count, so no information is lost — only the words around it.
 */
export function summarizeWorkCompact(rows: WorkLogEntry[]): string | null {
	const entries = rows.filter((entry) => !isHiddenCodeModeCell(entry.message));
	const tools = entries.filter(
		(entry) => entry.message.type === 'tool_use' || entry.message.type === 'subagent'
	);
	const thoughtCount = entries.filter(
		(entry) => entry.message.type === 'thinking'
	).length;
	const compactions = entries.filter((entry) => isCompactionCard(entry.message)).length;
	const deliveries = entries.filter((entry) => agentDelivery(entry.message)).length;
	if (tools.length === 0 && thoughtCount === 0 && compactions === 0 && deliveries === 0) return null;

	const counts = new Map<ToolAction, number>();
	let failed = 0;
	for (const entry of tools) {
		const action = toolAction(entry.message);
		counts.set(action, (counts.get(action) ?? 0) + 1);
		if (entry.message.toolStatus === 'error') failed++;
	}
	const tokens = [...counts.entries()].map(([action, count]) =>
		COMPACT_ACTION_LABELS[action](count)
	);
	if (thoughtCount > 0) tokens.push(`Thought ${thoughtCount}`);
	if (compactions > 0) tokens.push(`Compacted ${compactions}`);
	if (deliveries > 0) tokens.push(`Msgs ${deliveries}`);
	if (failed > 0) tokens.push(`${failed} failed`);
	return tokens.length > 0 ? tokens.join(' · ') : null;
}

/**
 * A code-mode cell has no row of its own in the transcript unless it failed, so
 * it must not appear in the fold's tally either — only the tool calls made from
 * inside it are real work, and those arrive as their own rows while the cell is
 * still running. A failed cell renders, and counts.
 */
function isHiddenCodeModeCell(message: ChatMessage): boolean {
	if (message.type !== 'tool_use' || message.toolName !== 'run_python') return false;
	return message.toolStatus !== 'error';
}

/** Provider-neutral, action-specific summary for a folded tool run. */
export function summarizeWork(rows: WorkLogEntry[]): string | null {
	const entries = rows.filter((entry) => !isHiddenCodeModeCell(entry.message));
	const tools = entries.filter(
		(entry) => entry.message.type === 'tool_use' || entry.message.type === 'subagent'
	);
	const compactions = entries.filter((entry) => isCompactionCard(entry.message)).length;
	const compactionLabel = compactions === 1 ? 'compacted the context' : `compacted the context ${compactions} times`;
	const deliveries = entries.filter((entry) => agentDelivery(entry.message)).length;
	if (tools.length === 0 && deliveries === 0) {
		const base = entries.some((entry) => entry.message.type === 'thinking')
			? 'Reasoning and interim work'
			: entries.length > compactions ? 'Interim work' : null;
		if (compactions === 0) return base;
		return base ? `${base}, ${compactionLabel}` : compactionLabel.charAt(0).toUpperCase() + compactionLabel.slice(1);
	}

	const counts = new Map<ToolAction, number>();
	let failed = 0;
	for (const entry of tools) {
		const action = toolAction(entry.message);
		counts.set(action, (counts.get(action) ?? 0) + 1);
		if (entry.message.toolStatus === 'error') failed++;
	}

	// Delegation closes the sentence: "Read 3 files and delegated 2 tasks".
	const ordered = [...counts].sort(
		(a, b) => Number(a[0] === 'delegate') - Number(b[0] === 'delegate')
	);
	// Thinking folds too; without a mention the reader has no way to know it
	// happened without expanding the fold.
	const thoughtCount = entries.filter((entry) => entry.message.type === 'thinking').length;
	const labels = ordered.map(([action, count]) => actionLabel(action, count));
	if (thoughtCount > 0) {
		labels.push(`thought ${thoughtCount} ${thoughtCount === 1 ? 'time' : 'times'}`);
	}
	if (compactions > 0) labels.push(compactionLabel);
	if (deliveries > 0) labels.push(`Got ${deliveries} agent ${deliveries === 1 ? 'message' : 'messages'}`);
	const sentence = sentenceJoin(labels);
	if (!sentence) return null;
	return failed > 0 ? `${sentence}, ${failed} failed` : sentence;
}

interface Positioned {
	message: ChatMessage;
	index: number;
}

/**
 * Split a deduplicated list into turns, each beginning at a user message.
 *
 * A prompt steered into a turn that is already running belongs to that turn: it
 * carries the running turn's id instead of opening one of its own, and it sits
 * wherever the model received it (right after the tool step that delivered it).
 * Starting a new turn there would cut the turn in two and re-anchor its duration
 * to the steer, so a user message only opens a turn when its id differs from the
 * one the current turn started with. Messages with no turn id (optimistic sends,
 * legacy transcripts) keep the plain one-turn-per-user-message rule.
 */
function splitTurns(messages: Positioned[]): Positioned[][] {
	const turns: Positioned[][] = [];
	let current: Positioned[] = [];
	let currentTurnId: string | undefined;
	for (const entry of messages) {
		// Only an authoritative idle wake opens a peer-only turn. Different wake
		// ids while parked/live still belong to the ongoing logical user turn.
		const idleAgentWake = !!agentDelivery(entry.message) && entry.message.metadata?.agent_wake === true;
		if (idleAgentWake || (entry.message.role === 'user' && !agentDelivery(entry.message))) {
			const turnId = entry.message.turnId;
			const steeredIntoCurrentTurn = !idleAgentWake && !!turnId && turnId === currentTurnId && current.length > 0;
			if (!steeredIntoCurrentTurn) {
				if (current.length > 0) {
					turns.push(current);
					current = [];
				}
				currentTurnId = turnId;
			}
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
	/** Main-lane streaming state; live turns render without work folds. */
	isStreaming: boolean;
	/**
	 * Authoritative presentation lifecycle for the latest turn. This is true for
	 * streaming, parked background work, and pending decision surfaces. When
	 * omitted, legacy callers fall back to `isStreaming`.
	 */
	latestTurnUnsettled?: boolean;
	/** User preference: false renders work rows flat, with no folds. */
	groupingEnabled: boolean;
	/** Session-scoped live/hydrated workflows, including updates before tool.result. */
	swarms?: readonly SwarmSnapshot[];
	sessionId?: string | null;
}

/** Remove only delegation rows whose exact identities belong to a rendered Swarm. */
function swarmOwnedMessages(messages: ChatMessage[], options: BuildOptions): Set<ChatMessage> {
	const launches = new Set<string>();
	const tasks = new Set<string>();
	const workers = new Set<string>();
	const snapshots = (options.swarms ?? []).map(parseSwarm).filter(
		(s): s is SwarmSnapshot => !!s && (!options.sessionId || s.session_id === options.sessionId)
	);
	for (const message of messages) {
		if (message.type !== 'tool_use' || !isSwarmTool(message.toolName ?? '')) continue;
		if (message.toolId) launches.add(message.toolId);
		const parsed = parseSwarm(message.toolResult);
		const initial = parsed && (!options.sessionId || parsed.session_id === options.sessionId) ? parsed : null;
		// Use the same workflow association as SwarmCard, then join children by
		// execution IDs, never task labels, agent names, or proximity in the turn.
		const snapshot = resolveSwarm(snapshots, initial, message.toolInput);
		for (const task of snapshot?.tasks ?? []) {
			if (task.task_id) tasks.add(task.task_id);
			if (task.worker_session_id) workers.add(task.worker_session_id);
		}
	}
	const hidden = new Set<ChatMessage>();
	const toolIds = new Set([...tasks, ...workers, ...launches]);
	for (const message of messages) {
		if (message.type !== 'subagent') continue;
		if (
			(message.toolId && (tasks.has(message.toolId) || workers.has(message.toolId) || launches.has(message.toolId))) ||
			(message.agentSessionId && workers.has(message.agentSessionId)) ||
			(message.agentId && workers.has(message.agentId)) ||
			(message.agentLaunchToolUseId && launches.has(message.agentLaunchToolUseId))
		) {
			hidden.add(message);
			if (message.toolId) toolIds.add(message.toolId);
			if (message.agentLaunchToolUseId) toolIds.add(message.agentLaunchToolUseId);
		}
	}
	for (const message of messages) {
		if (!message.toolId || !toolIds.has(message.toolId)) continue;
		const name = message.toolName?.split('__').at(-1)?.toLowerCase();
		if (message.type === 'tool_result' || (message.type === 'tool_use' && (name === 'task' || name === 'agent'))) {
			hidden.add(message);
		}
	}
	return hidden;
}

function emitLiveTurn(turn: Positioned[], items: DisplayItem[]): void {
	// Code-mode wrappers are not transcript rows: their dispatched tools are.
	// Keep a streaming cell visible only until its first visible inner call arrives.
	const lastVisibleIndex = turn.findLastIndex((entry) => !isHiddenCodeModeCell(entry.message));
	const streamingCell = turn.findLast(
		(entry) => isHiddenCodeModeCell(entry.message) && entry.message.streaming === true
	);
	for (const [index, entry] of turn.entries()) {
		if (
			isHiddenCodeModeCell(entry.message) &&
			!(entry === streamingCell && index > lastVisibleIndex)
		) continue;
		// Nothing in the current turn folds, even between tool runs or while parked.
		items.push({ kind: 'message', message: entry.message, index: entry.index });
	}
}

/**
 * Build the transcript display list without changing store or protocol semantics.
 *
 * Thin wrapper over `buildTranscriptModel` for callers (and tests) that only
 * want the grouped list.
 */
export function buildDisplayItems(messages: ChatMessage[], options: BuildOptions): DisplayItem[] {
	return buildTranscriptModel(messages, options).items;
}

/**
 * Linear passes over `messages` producing the grouped display list AND the two
 * whole-array lookups the template used to recompute per rendered row.
 *
 * The walk that de-duplicates ids and drops assistant phantoms already touches
 * every message, so the prompt-occurrence counter and the subagent-linked tool
 * ids ride along on it: O(N) once instead of O(N) per row per fold.
 */
/** Per-view cache for the common immutable streaming-tail update. Historical
 * identity, lifecycle, metadata, and Swarm changes all take the full builder.
 * Never cache by message id: completed tools and late Swarm association can
 * change earlier rows, even while the final assistant paragraph streams.
 */
export function createTranscriptModelBuilder() {
	let previous: { messages: ChatMessage[]; options: BuildOptions; model: TranscriptModel } | undefined;
	return (messages: ChatMessage[], options: BuildOptions): TranscriptModel => {
		const old = previous;
		const tail = messages.at(-1);
		const oldTail = old?.messages.at(-1);
		const lastItem = old?.model.items.at(-1);
		const swarms = options.swarms ?? [];
		const oldSwarms = old?.options.swarms ?? [];
		const sameOptions = old && options.sessionId === old.options.sessionId
			&& options.isStreaming === old.options.isStreaming
			&& options.latestTurnUnsettled === old.options.latestTurnUnsettled
			&& options.groupingEnabled === old.options.groupingEnabled
			&& swarms.length === oldSwarms.length && swarms.every((s, i) => s === oldSwarms[i]);
		let model: TranscriptModel;
		if (sameOptions && (options.latestTurnUnsettled ?? options.isStreaming)
			&& tail && oldTail && tail.streaming && oldTail.streaming
			&& tail.role === 'assistant' && ['text', 'thinking'].includes(tail.type ?? '')
			&& tail.content && oldTail.content && !isAssistantPhantom(tail)
			&& lastItem?.kind === 'message' && lastItem.message === oldTail
			&& lastItem.index === messages.length - 1
			&& messages.length === old.messages.length
			&& messages.every((message, i) => i === messages.length - 1 || message === old.messages[i])
			&& Object.keys(tail).length === Object.keys(oldTail).length
			&& Object.keys(tail).every(key => key === 'content'
				|| tail[key as keyof ChatMessage] === oldTail[key as keyof ChatMessage])) {
			const items = old.model.items.slice();
			items[items.length - 1] = { ...lastItem, message: tail };
			model = { ...old.model, items };
		} else {
			model = buildTranscriptModel(messages, options);
		}
		previous = { messages, options, model };
		return model;
	};
}

export function buildTranscriptModel(
	messages: ChatMessage[],
	options: BuildOptions
): TranscriptModel {
	const { isStreaming, groupingEnabled } = options;
	const latestTurnUnsettled = options.latestTurnUnsettled ?? isStreaming;
	const swarmOwned = swarmOwnedMessages(messages, options);

	// Duplicate ids crash Svelte's keyed each; drop repeats defensively.
	const seen = new Set<string>();
	const positioned: Positioned[] = [];
	// Prompt-occurrence bookkeeping: how many earlier prompts carried the exact
	// same trimmed text. Counted over the RAW array (phantom/duplicate filtering
	// below must not shift the ordinals the rewind API resolves against).
	const promptOccurrence = new Array<number>(messages.length).fill(0);
	const promptCounts = new Map<string, number>();
	const promptTurnIds = new Set<string>();
	const rewindEligible = new Array<boolean>(messages.length).fill(false);
	const subagentToolIds = new Set<string>();
	let lastAssistantMetadata: Record<string, unknown> | null = null;
	let latestUserTurnId: string | null = null;
	messages.forEach((message, index) => {
		const realTurnId = message.turnId && message.turnId !== 'backfill' ? message.turnId : null;
		rewindEligible[index] = canRewindUserMessage(message, !!realTurnId && promptTurnIds.has(realTurnId));
		if (message.role === 'user' && message.type !== 'command' && !agentDelivery(message)) {
			if (realTurnId) promptTurnIds.add(realTurnId);
			if (message.id) latestUserTurnId = message.id;
		}
		if (rewindEligible[index]) {
			const key = (message.content ?? '').trim();
			const count = promptCounts.get(key) ?? 0;
			promptOccurrence[index] = count;
			promptCounts.set(key, count + 1);
		} else if (message.type === 'subagent') {
			subagentToolIds.add(message.toolId ?? NO_TOOL_ID);
		}
		if (message.role === 'assistant' && message.metadata) {
			lastAssistantMetadata = message.metadata as Record<string, unknown>;
		}
		// Empty assistant placeholders intentionally have no visual row. Keeping them
		// in this model can split the final live tool run and hide its active tail.
		if (
			isAssistantPhantom(message) ||
			swarmOwned.has(message) ||
			isTransientProviderRetry(message)
		) return;
		if (message.id) {
			if (seen.has(message.id)) return;
			seen.add(message.id);
		}
		positioned.push({ message, index });
	});

	const items: DisplayItem[] = [];
	const turns = splitTurns(positioned);

	turns.forEach((turn, turnIndex) => {
		if (!groupingEnabled) {
			emitWithSystemGroups(turn, items);
			return;
		}

		const unsettledTurn = latestTurnUnsettled && turnIndex === turns.length - 1;
		if (unsettledTurn) {
			emitLiveTurn(turn, items);
			return;
		}

		// Ordinary preamble/history stays flat. A peer wake can start work without
		// a human prompt; its delivery belongs inside that turn's disclosure.
		const userIndex = turn.findIndex((entry) => entry.message.role === 'user' && !agentDelivery(entry.message));
		if (userIndex < 0 && !turn.some((entry) => agentDelivery(entry.message))) {
			emitWithSystemGroups(turn, items);
			return;
		}

		// A steer does not end the running turn, but it is a presentation boundary
		// once that turn settles. Keep its work and media on the side they arrived.
		const turnMessages = turn.map((entry) => entry.message);
		const durationLabel = turnFoldLabel(turnMessages, turnWasInterrupted(turnMessages));
		const segments: Positioned[][] = [[]];
		for (const entry of turn) {
			if (entry.message.role === 'user' && !agentDelivery(entry.message) && segments.at(-1)!.length > 0) segments.push([]);
			segments.at(-1)!.push(entry);
		}
		for (const segment of segments) {
			const prefix = userIndex < 0 ? [] : segment.slice(0, 1);
			const response = segment.slice(prefix.length);
			const gallery = galleryMediaForTurn(response.map((entry) => entry.message), options.sessionId);
			const terminalAssistant = response.findLast((entry) => isAssistantText(entry.message)) ?? null;
			// A compaction card the turn worked past is mid-turn; one trailing the
			// answer (an idle or manual /compact) keeps its own row.
			const lastWork = response.findLastIndex((entry) => isTurnWork(entry.message));
			const midTurnCompactions = new Set(
				response.filter((entry, i) => i < lastWork && isCompactionCard(entry.message))
			);
			const shouldHide = (entry: Positioned) =>
				isCompletedRoutineWork(entry.message, gallery.length > 1) ||
				isGroupableSystem(entry.message) ||
				midTurnCompactions.has(entry) ||
				entry.message.type === 'tool_result' ||
				(isAssistantText(entry.message) && !entry.message.streaming && entry !== terminalAssistant);
			const folded = response
				.filter(shouldHide)
				.map((entry) => ({ message: entry.message, index: entry.index }));

			if (folded.length === 0 && gallery.length === 0) {
				emitWithSystemGroups(segment, items);
				continue;
			}

			// User prompts stay outside disclosures, including steers. The duration
			// still describes the logical turn; summaries count only this segment.
			emitWithSystemGroups(prefix, items);
			// Keyed by the prompt that opens the segment, which never changes. Keying
			// by the first folded row remounted the group (collapsing an open log and
			// replaying its settle animation) whenever a late tool result or a
			// finished delegation folded an earlier row.
			const anchor = prefix[0] ?? folded[0] ?? response[0];
			items.push({
				kind: 'work_log',
				key: `wl-turn-${anchor.message.id ?? anchor.index}`,
				gallery,
				mode: 'settled',
				folded,
				tail: [],
				sequence: response.map((entry) => ({
					message: entry.message,
					index: entry.index,
					hiddenWhenCollapsed: shouldHide(entry)
				})),
				durationLabel,
				summary: summarizeWork(folded),
				compactSummary: summarizeWorkCompact(folded)
			});
		}
	});

	// Turn navigation jumps by message id; windowed mounting needs to know which
	// display item that id lives in so it can mount far enough back to reach it.
	const itemIndexByTurnId = new Map<string, number>();
	items.forEach((item, itemIndex) => {
		// Mid-turn prompts remain in the settled work log's ordered sequence.
		const entries = item.kind === 'message' ? [item] : item.kind === 'work_log' ? item.sequence : [];
		for (const { message } of entries) {
			if (message.id && isNavigableUserTurn(message) && !itemIndexByTurnId.has(message.id)) {
				itemIndexByTurnId.set(message.id, itemIndex);
			}
		}
	});

	return {
		items,
		promptOccurrence,
		rewindEligible,
		subagentToolIds,
		itemIndexByTurnId,
		lastAssistantMetadata,
		latestUserTurnId
	};
}
