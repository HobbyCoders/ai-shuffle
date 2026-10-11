/**
 * Transcript presentation model.
 *
 * AI Shuffle keeps the wire/store model flat and derives the compact transcript
 * here. The rules deliberately mirror T3 Code's readable turn rhythm without
 * adopting its data model:
 *
 * - assistant prose is never withheld while a turn is live;
 * - a live turn folds its completed routine work run by run, between the prose
 *   and protected rows that stay on screen; the work after the latest prose
 *   stays flat until the turn settles;
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
	/**
	 * The row's media joined the fold's gallery, so a retained copy of it may
	 * drop the duplicate. Rows after a steer never contribute, so they keep
	 * every image even when it repeats one shown in the gallery.
	 */
	inGallery?: boolean;
	/** Live turn only: the run this row belongs to (see `WorkLogRun`). */
	run?: number;
	/** Live turn only: the row hides while its run is folded. */
	foldsInRun?: boolean;
}

/**
 * One earlier run of a live turn (spec §2): the routine rows between two
 * boundaries (protected rows and the prose that stays on screen). It renders as one live fold; the protected row that ended it
 * stays visible below. The current run (still receiving rows) has no entry.
 */
export interface WorkLogRun {
	/** Position among all runs of the turn, counting runs with nothing to fold. */
	index: number;
	/**
	 * Stable fold-state key: the id of the protected row that ended the run.
	 * The ordinal shifts when a new boundary appears earlier in the turn (prose
	 * that finished streaming); the row that ended a run does not, so a
	 * run the reader opened stays open on the same work.
	 */
	key: string;
	/** Rows hidden while the run is folded. */
	folded: WorkLogEntry[];
	summary: string | null;
	compactSummary: string | null;
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
			/** Live turn: its earlier runs, each folded on its own (spec §2). */
			runs?: WorkLogRun[];
			/**
			 * Fold-state key of the turn (spec §6): the server id of the row that
			 * opened it. Run folds are `<foldKey>:run-<run.key>`, the settled group `<foldKey>:turn`.
			 */
			foldKey?: string;
			/** The optimistic id `foldKey` replaced, if any: its folds are remapped. */
			foldAlias?: string;
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
	/**
	 * Ids of user messages steered into a turn already under way: same turn id
	 * as the turn's opening prompt, but not that prompt. Their time line reads
	 * "· mid-turn".
	 */
	midTurnUserIds: ReadonlySet<string>;
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
 * A row that is not prose and never folds in a settled turn (spec §3): a worker
 * card, a decision-bearing tool (questions, todos, Task/Agent, swarm), or a
 * system notice that is not routine bookkeeping. Assistant mode keeps the same
 * rows inline. A compaction card counts too: one trailing the answer keeps its
 * row, and a mid-turn one is still folded by the settled turn's
 * `hiddenWhenCollapsed`.
 */
export function isProtectedRow(msg: ChatMessage): boolean {
	if (agentDelivery(msg)) return false;
	if (msg.type === 'subagent') return true;
	if (msg.type === 'tool_use') return isSpecialTool(msg);
	if (msg.type !== 'system' && msg.role !== 'system') return false;
	return !isGroupableSystem(msg) && !isFoldableRetryCard(msg);
}

/** A tool that puts a question to the reader. */
function isAskingTool(msg: ChatMessage): boolean {
	return msg.type === 'tool_use' && (msg.toolName === 'AskUserQuestion' || msg.toolName === 'AskUser');
}

/**
 * Prose the reader must see even after the turn settles (transcript-organization
 * spec §3): it asks something, or it carries a finding (two or more lines, or a
 * heading, list, table or code fence). One short line without a question is
 * narration ("Now checking the workflow.") and folds with the work.
 */
export function isProtectedProse(content: string): boolean {
	const text = content.trim();
	if (!text) return false;
	if (text.split('\n').filter((line) => line.trim()).length >= 2) return true;
	if (/^\s{0,3}(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```|~~~)/m.test(text)) return true;
	return asksQuestion(text);
}

/**
 * Prose that asks the reader something (spec §3's question rule, shared by
 * `isProtectedProse` and the "Needs you" count, spec §5). A question mark
 * inside a link target, a bare URL or inline code is not a question;
 * punctuation that ends the sentence after a URL still counts.
 */
export function asksQuestion(content: string): boolean {
	const prose = content
		.replace(/`[^`]*`/g, '')
		.replace(/\]\([^)\s]*\)/g, ']')
		.replace(/<https?:\/\/[^>\s]*>/g, '')
		.replace(/\bhttps?:\/\/\S*[^\s.,;:!?)\]'"]/g, '');
	return prose.includes('?');
}

/**
 * Whether the conversation is waiting on the reader's answer to a prose
 * question (spec §5): the newest assistant prose since the last user message
 * asks something. A user message (prompt, steer or command) answers it; an
 * agent's delivered report does not. Prose still streaming has not finished
 * asking, so it neither counts nor lets an earlier question count. Prose that
 * leads into a question tool is that tool's question, which the question
 * surface already counts once.
 */
export function lastProseAsksQuestion(messages: readonly ChatMessage[]): boolean {
	for (let index = messages.length - 1; index >= 0; index--) {
		const message = messages[index];
		if (message.role === 'user' && !agentDelivery(message)) return false;
		if (isAskingTool(message)) return false;
		if (!isAssistantText(message)) continue;
		return !message.streaming && asksQuestion(message.content.trim());
	}
	return false;
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
	// A worker card is the delegation itself: it keeps its row in every status
	// and carries the head of the worker's report (spec §3, §4).
	if (msg.type === 'subagent') return false;
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
	| 'message'
	| 'task-check'
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
	// Agent-task plumbing a coordinator turn is full of: messages to peers and
	// subagents, and checks on background tasks. Neither reads a file.
	if (['SendMessage', 'MessageSession'].includes(name)) return 'message';
	if (['TaskGet', 'TaskOutput'].includes(name)) return 'task-check';
	if (['Read', 'BashOutput'].includes(name)) return 'read';
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
		case 'message':
			return `Sent ${count} ${count === 1 ? 'message' : 'messages'}`;
		case 'task-check':
			return `Checked ${count} ${count === 1 ? 'task' : 'tasks'}`;
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
	message: (n) => `Sent ${n}`,
	'task-check': (n) => `Checked ${n}`,
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
	const taskResults = entries.filter((entry) => isTaskResult(entry.message)).length;
	if (tools.length === 0 && thoughtCount === 0 && compactions === 0 && deliveries === 0 && taskResults === 0) return null;

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
	if (taskResults > 0) tokens.push(`Results ${taskResults}`);
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

/**
 * A background task's completion notice ("Task COMPLETED"). It is the result
 * of work launched earlier, so a fold counts it; the started/progress/updated
 * notices around it are bookkeeping and only fold.
 */
function isTaskResult(message: ChatMessage): boolean {
	return message.type === 'system' && message.systemSubtype === 'task_notification';
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
	const taskResults = entries.filter((entry) => isTaskResult(entry.message)).length;
	if (tools.length === 0 && deliveries === 0 && taskResults === 0) {
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
	if (taskResults > 0) labels.push(`Got ${taskResults} task ${taskResults === 1 ? 'result' : 'results'}`);
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

/** Stable identity of a turn: the row that opens it (what its fold is keyed by). */
function turnKeyOf(turn: Positioned[]): string {
	const first = turn[0];
	return first.message.id ?? String(first.index);
}

/**
 * Display key and fold-state keys of a turn's work log. The display key uses
 * the row's own id, which an optimistic send keeps after reconcile, so the
 * group never remounts; the fold key prefers the server id a reload will see.
 */
function workLogKeys(anchor: Positioned): Pick<Extract<DisplayItem, { kind: 'work_log' }>, 'key' | 'foldKey' | 'foldAlias'> {
	const own = anchor.message.id ?? String(anchor.index);
	const foldKey = anchor.message.serverId ?? own;
	return { key: `wl-turn-${own}`, foldKey, ...(foldKey !== own ? { foldAlias: own } : {}) };
}

/**
 * Identity of the latest turn by the same rule as `splitTurns` and `turnKeyOf`,
 * without building the transcript. The settle hand-off (settle-hold.ts) tracks
 * it to notice the moment a send or wake opens a newer turn.
 */
export function latestTurnKey(messages: readonly ChatMessage[]): string | null {
	let key: string | null = null;
	let currentTurnId: string | undefined;
	messages.forEach((message, index) => {
		const idleAgentWake = !!agentDelivery(message) && message.metadata?.agent_wake === true;
		const opens = idleAgentWake || (message.role === 'user' && !agentDelivery(message));
		if (key === null || (opens && !(!idleAgentWake && !!message.turnId && message.turnId === currentTurnId))) {
			key = message.id ?? String(index);
			if (opens) currentTurnId = message.turnId;
		}
	});
	return key;
}

/**
 * The turn id an optimistic send should carry so `splitTurns` keeps it inside
 * the turn it interrupts.
 *
 * Only for a send into a PARKED turn (not streaming, but background work still
 * running): the turn is still going, so the message is a steer and must not
 * open a turn of its own and fold everything above it. The id is the live turn
 * when known, otherwise the id of the message that opened the latest turn —
 * the same rule `splitTurns` uses — and only when that turn is the parked one:
 * the reducer says so (`turnParked` is set by the park gate for the turn that
 * just completed and cleared by the next `turn.started`), or a running task's
 * launch row sits inside it. Running work launched by an EARLIER turn does not
 * make the latest, completed turn parked. The server's `message.user` replaces
 * the id once the backend has started the turn that carries the message (it
 * follows that turn's `turn.started`), which is the only moment the row moves.
 */
export function parkedSendTurnId(state: {
	isStreaming: boolean;
	liveTurnId?: string | null;
	turnParked?: boolean;
	activeTasks?: readonly { status: string; toolUseId?: string | null }[];
	messages: readonly ChatMessage[];
}): string | undefined {
	if (state.isStreaming) return undefined;
	const runningLaunches = new Set(
		(state.activeTasks ?? [])
			.filter((task) => task.status === 'running' && !!task.toolUseId)
			.map((task) => task.toolUseId as string)
	);
	if (state.turnParked !== true && runningLaunches.size === 0) return undefined;
	if (state.liveTurnId) return state.liveTurnId;
	let ownsRunningWork = false;
	for (let index = state.messages.length - 1; index >= 0; index--) {
		const message = state.messages[index];
		const opensTurn = (!!agentDelivery(message) && message.metadata?.agent_wake === true) ||
			(message.role === 'user' && !agentDelivery(message));
		if (opensTurn) {
			if (state.turnParked !== true && !ownsRunningWork) return undefined;
			return message.turnId && message.turnId !== 'backfill' ? message.turnId : undefined;
		}
		if (message.toolId && runningLaunches.has(message.toolId)) ownsRunningWork = true;
	}
	return undefined;
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
	/**
	 * Launch tool ids of background work still running. A turn that launched
	 * one has something pending, so it stays unsettled (flat) even after a
	 * later turn opened — a send or wake must not fold work still in flight.
	 */
	runningToolIds?: ReadonlySet<string>;
	/**
	 * Turns (by opening row id, see `latestTurnKey`) still inside their settle
	 * hold after a newer turn opened. A send during the hold must not cut it
	 * short: the turn stays flat until its own hold runs out.
	 */
	heldTurnKeys?: ReadonlySet<string>;
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

const isPromptRow = (entry: Positioned) => entry.message.role === 'user' && !agentDelivery(entry.message);

/**
 * Prose the reader must see while it is the latest word, or because it asks:
 * a lead-in followed by a question tool (before the next prose or steer) asks
 * too. Shared by the live runs and the settled turn so both keep the same rows.
 */
function askingLeadIns(response: Positioned[]): Set<Positioned> {
	const asking = new Set<Positioned>();
	let pendingLeadIn: Positioned | null = null;
	for (const entry of response) {
		if (isPromptRow(entry)) pendingLeadIn = null;
		else if (isAssistantText(entry.message)) pendingLeadIn = entry;
		else if (pendingLeadIn && isAskingTool(entry.message)) asking.add(pendingLeadIn);
	}
	return asking;
}

/**
 * The live turn (spec §2). Grouping on, it becomes the opening prompt plus ONE
 * work log in live mode, keyed exactly as the settled group of the same turn
 * will be, so settling flips `mode` on the same component instead of mounting
 * a new one (spec §6). Inside it the turn is cut into runs at every protected
 * row and at the completed prose that stays on screen: earlier runs fold
 * behind the live label, the row that ended each stays visible below it, and
 * the current run shows every row.
 */
/**
 * The last prose before each steer is the answer the steer replied to, the
 * first prose after each steer is the reply to the steer, and the last prose
 * of the turn is its answer: all stay on screen, live and settled. When a
 * steer is followed only by tools and then the turn's last prose, that prose
 * is both, so it is kept once.
 */
function terminalProse(response: Positioned[]): Set<Positioned> {
	const terminal = new Set<Positioned>();
	let lastProse: Positioned | null = null;
	let awaitingReply = false;
	for (const entry of response) {
		if (entry.message.role === 'user' && !agentDelivery(entry.message)) {
			if (lastProse) terminal.add(lastProse);
			lastProse = null;
			awaitingReply = true;
		} else if (isAssistantText(entry.message)) {
			if (awaitingReply) terminal.add(entry);
			awaitingReply = false;
			lastProse = entry;
		}
	}
	if (lastProse) terminal.add(lastProse);
	return terminal;
}

/** Fold-state key of a run, from the protected row that ended it (see `WorkLogRun.key`). */
function runKeyOf(entry: Positioned): string {
	return entry.message.serverId ?? entry.message.id ?? `at-${entry.index}`;
}

function emitLiveTurn(turn: Positioned[], items: DisplayItem[]): void {
	// Code-mode wrappers are not transcript rows: their dispatched tools are.
	// Keep a streaming cell visible only until its first visible inner call arrives.
	const lastVisibleIndex = turn.findLastIndex((entry) => !isHiddenCodeModeCell(entry.message));
	const streamingCell = turn.findLast(
		(entry) => isHiddenCodeModeCell(entry.message) && entry.message.streaming === true
	);
	const shown = turn.filter((entry, index) =>
		!isHiddenCodeModeCell(entry.message) || (entry === streamingCell && index > lastVisibleIndex)
	);

	// Same opening-row rule as the settled turn, so both pick the same anchor.
	const userIndex = turn.findIndex(isPromptRow);
	if (userIndex < 0 && !turn.some((entry) => agentDelivery(entry.message))) {
		for (const entry of shown) items.push({ kind: 'message', message: entry.message, index: entry.index });
		return;
	}
	const prefix = userIndex < 0 ? [] : turn.slice(0, 1);
	const response = shown.filter((entry) => entry !== prefix[0]);
	emitWithSystemGroups(prefix, items);
	if (response.length === 0) return;

	const asking = askingLeadIns(response);
	// The latest prose and the prose each steer replied to stay on screen
	// whatever they say, exactly as the settled turn keeps them.
	const terminal = terminalProse(response);
	// The newest prose the agent has FINISHED writing. Prose still streaming
	// has not replaced it yet, so the run it ends stays folded while the next
	// paragraph arrives instead of opening up for a moment and folding again.
	const latestDone = response.findLast((entry) => isAssistantText(entry.message) && !entry.message.streaming);
	// Rows that end a run and stay visible. Streaming prose is still arriving,
	// so it only ends a run once it is complete. A steer does NOT end a run: a
	// turn never folds because the user sent something (spec §1.3); it stays a
	// visible row in place inside whatever run is current. The agent's own
	// completed prose does when the reader must keep seeing it: a finding, a
	// question's lead-in, the latest word, or the prose a steer answered or
	// that answers a steer. The routine work behind it folds, so a long live
	// turn reads as prose between folds; only the work after the latest prose
	// stays flat. A failed tool is routine work: it folds with its run and the
	// run's summary counts it ("…, 1 failed").
	const endsRun = (entry: Positioned) =>
		isProtectedRow(entry.message) ||
		asking.has(entry) ||
		(isAssistantText(entry.message) && !entry.message.streaming && (
			isProtectedProse(entry.message.content) || entry === latestDone || terminal.has(entry)
		));
	// Rows a folded run hides; anything else (a tool still running, a
	// screenshot, streaming thinking) keeps its place between the folds.
	const foldsWithRun = (entry: Positioned) =>
		!terminal.has(entry) && (
			isCompletedRoutineWork(entry.message) ||
			isGroupableSystem(entry.message) ||
			entry.message.type === 'tool_result' ||
			(isAssistantText(entry.message) && !entry.message.streaming)
		);

	const runs: WorkLogRun[] = [];
	const runOf = new Map<Positioned, number>();
	const hidden = new Set<Positioned>();
	let runIndex = 0;
	let open: Positioned[] = [];
	for (const entry of response) {
		runOf.set(entry, runIndex);
		if (!endsRun(entry)) {
			open.push(entry);
			continue;
		}
		const folded = open.filter(foldsWithRun);
		for (const row of folded) hidden.add(row);
		if (folded.length > 0) {
			const rows = folded.map(({ message, index }) => ({ message, index }));
			runs.push({ index: runIndex, key: runKeyOf(entry), folded: rows, summary: summarizeWork(rows), compactSummary: summarizeWorkCompact(rows) });
		}
		runIndex++;
		open = [];
	}

	const folded = response.filter((entry) => hidden.has(entry)).map(({ message, index }) => ({ message, index }));
	items.push({
		kind: 'work_log',
		...workLogKeys(prefix[0] ?? response[0]),
		mode: 'live',
		folded,
		tail: [],
		// `hiddenWhenCollapsed` describes the settled fold; a live turn hides
		// nothing there (assistant mode reads it), only inside its folded runs.
		sequence: response.map((entry) => ({
			message: entry.message,
			index: entry.index,
			hiddenWhenCollapsed: false,
			run: runOf.get(entry),
			...(hidden.has(entry) ? { foldsInRun: true } : {})
		})),
		runs,
		durationLabel: null,
		summary: summarizeWork(folded),
		compactSummary: summarizeWorkCompact(folded)
	});
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
function sameIds(a: ReadonlySet<string> | undefined, b: ReadonlySet<string> | undefined): boolean {
	if (a === b) return true;
	if ((a?.size ?? 0) !== (b?.size ?? 0)) return false;
	for (const id of a ?? []) if (!b?.has(id)) return false;
	return true;
}

export function createTranscriptModelBuilder() {
	// `itemAt` maps raw index → item index for exactly `model` (card-progress patch path).
	let previous: { messages: ChatMessage[]; options: BuildOptions; model: TranscriptModel; itemAt?: Int32Array } | undefined;
	const turnCache = createTurnCache();
	const build = (messages: ChatMessage[], options: BuildOptions): TranscriptModel => {
		let changed: number | null = null;
		let itemAt: Int32Array | undefined;
		const old = previous;
		const tail = messages.at(-1);
		const oldTail = old?.messages.at(-1);
		const lastItem = old?.model.items.at(-1);
		const swarms = options.swarms ?? [];
		const oldSwarms = old?.options.swarms ?? [];
		const sameOptions = old && options.sessionId === old.options.sessionId
			&& options.isStreaming === old.options.isStreaming
			&& options.latestTurnUnsettled === old.options.latestTurnUnsettled
			&& sameIds(options.runningToolIds, old.options.runningToolIds)
			&& sameIds(options.heldTurnKeys, old.options.heldTurnKeys)
			&& options.groupingEnabled === old.options.groupingEnabled
			&& swarms.length === oldSwarms.length && swarms.every((s, i) => s === oldSwarms[i]);
		let model: TranscriptModel;
		const onlyTailTextGrew = !!sameOptions && (options.latestTurnUnsettled ?? options.isStreaming)
			&& !!tail && !!oldTail && !!tail.streaming && !!oldTail.streaming
			&& tail.role === 'assistant' && ['text', 'thinking'].includes(tail.type ?? '')
			&& !!tail.content && !!oldTail.content && !isAssistantPhantom(tail)
			&& messages.length === old.messages.length
			&& messages.every((message, i) => i === messages.length - 1 || message === old.messages[i])
			&& Object.keys(tail).length === Object.keys(oldTail).length
			&& Object.keys(tail).every(key => key === 'content'
				|| tail[key as keyof ChatMessage] === oldTail[key as keyof ChatMessage]);
		// A streaming row never ends a live run and never folds, so its growing
		// text cannot change the runs: only the row itself is swapped.
		const lastRow = lastItem?.kind === 'work_log' && lastItem.mode === 'live' ? lastItem.sequence.at(-1) : undefined;
		if (onlyTailTextGrew && lastItem?.kind === 'message' && lastItem.message === oldTail
			&& lastItem.index === messages.length - 1) {
			const items = old!.model.items.slice();
			items[items.length - 1] = { ...lastItem, message: tail! };
			model = { ...old!.model, items };
		} else if (onlyTailTextGrew && lastItem?.kind === 'work_log' && lastRow
			&& lastRow.message === oldTail && lastRow.index === messages.length - 1 && !lastRow.foldsInRun) {
			const sequence = lastItem.sequence.slice();
			sequence[sequence.length - 1] = { ...lastRow, message: tail! };
			const items = old!.model.items.slice();
			items[items.length - 1] = { ...lastItem, sequence };
			model = { ...old!.model, items };
		} else if (sameOptions && (changed = onlyCardProgressChanged(old!.messages, messages)) !== null) {
			itemAt = old!.itemAt ?? locateItems(old!.model.items, messages.length);
			model = patchRow(old!.model, itemAt, changed, messages[changed]);
		} else {
			model = buildTranscriptModel(messages, options, turnCache);
		}
		// The locator lives on `previous`, so it always describes the current
		// model: any other path (a rebuild, a session switch, a streamed tail)
		// drops it rather than keeping a switched-away model reachable.
		previous = { messages, options, model, itemAt };
		return model;
	};
	/** Test hook: models this builder still references (the current one only). */
	build.retainedModels = (): TranscriptModel[] => (previous ? [previous.model] : []);
	return build;
}

/**
 * Fields a worker's `subagent.progress` / completion changes on its card, none
 * of which the grouping reads: the card is a protected row in every status,
 * never folds, and its work summary counts it by type alone. A change confined
 * to these fields cannot move a fold, a run, a summary or a turn boundary.
 */
const CARD_PROGRESS_FIELDS = new Set<string>(['agentChildren', 'agentStatus']);

/**
 * Index of the ONE subagent card that changed between two transcripts in
 * nothing but `CARD_PROGRESS_FIELDS`, or null when anything else differs. One
 * pointer comparison per row — no grouping work — so a burst of progress on a
 * 4,000-row session no longer rebuilds the history model per update (#1282).
 */
function onlyCardProgressChanged(before: ChatMessage[], after: ChatMessage[]): number | null {
	if (before === after || before.length !== after.length) return null;
	let found = -1;
	// Progress targets a recent card: scan from the end, stop at a second change.
	for (let i = after.length - 1; i >= 0; i--) {
		if (after[i] === before[i]) continue;
		if (found !== -1) return null;
		found = i;
	}
	if (found === -1) return null;
	const next = after[found];
	const prev = before[found];
	if (next.type !== 'subagent' || prev.type !== 'subagent' || next.id !== prev.id) return null;
	const nextKeys = Object.keys(next);
	const prevKeys = Object.keys(prev);
	for (const key of nextKeys) {
		if (CARD_PROGRESS_FIELDS.has(key)) continue;
		if (next[key as keyof ChatMessage] !== prev[key as keyof ChatMessage]) return null;
	}
	for (const key of prevKeys) {
		if (!CARD_PROGRESS_FIELDS.has(key) && !(key in next)) return null;
	}
	return found;
}

/** Display-item index holding each raw message index (-1: not rendered). */
function locateItems(items: DisplayItem[], length: number): Int32Array {
	const itemAt = new Int32Array(length).fill(-1);
	const mark = (index: number, itemIndex: number) => {
		if (index >= 0 && index < length && itemAt[index] === -1) itemAt[index] = itemIndex;
	};
	items.forEach((item, itemIndex) => {
		if (item.kind === 'message') mark(item.index, itemIndex);
		else if (item.kind === 'system_group') for (const index of item.indices) mark(index, itemIndex);
		else {
			for (const entry of item.sequence) mark(entry.index, itemIndex);
			for (const entry of item.folded) mark(entry.index, itemIndex);
			for (const entry of item.tail) mark(entry.index, itemIndex);
		}
	});
	return itemAt;
}

function swapEntries<T extends WorkLogEntry>(entries: T[], index: number, message: ChatMessage): T[] {
	if (!entries.some((entry) => entry.index === index)) return entries;
	return entries.map((entry) => (entry.index === index ? { ...entry, message } : entry));
}

/** The previous model with one row's message swapped; every other item keeps its identity. */
function patchRow(model: TranscriptModel, itemAt: Int32Array, index: number, message: ChatMessage): TranscriptModel {
	const itemIndex = itemAt[index] ?? -1;
	// Not rendered (a Swarm owns it, or a duplicate id): nothing to redraw.
	if (itemIndex < 0) return model;
	const item = model.items[itemIndex];
	let patched: DisplayItem;
	if (item.kind === 'message') {
		patched = { ...item, message };
	} else if (item.kind === 'system_group') {
		patched = { ...item, messages: item.messages.map((m, i) => (item.indices[i] === index ? message : m)) };
	} else {
		patched = {
			...item,
			sequence: swapEntries(item.sequence, index, message),
			folded: swapEntries(item.folded, index, message),
			tail: swapEntries(item.tail, index, message),
			...(item.runs ? { runs: item.runs.map((run) => {
				const folded = swapEntries(run.folded, index, message);
				return folded === run.folded ? run : { ...run, folded };
			}) } : {})
		};
	}
	const items = model.items.slice();
	items[itemIndex] = patched;
	return { ...model, items };
}

/**
 * Display items each turn produced on the previous build, by turn key.
 *
 * A turn's items depend only on its own rows (message identity AND raw index),
 * whether it is live, and the build-wide options checked by `scope`. So when a
 * worker's progress replaces one subagent card, every other turn reuses its
 * items by identity: no re-grouping, no summaries, no gallery work, and the
 * keyed `{#each}` sees unchanged props for every untouched group (#1282).
 */
interface TurnCacheEntry {
	rows: Positioned[];
	unsettled: boolean;
	items: DisplayItem[];
}

export interface TurnCache {
	scope: { groupingEnabled: boolean; sessionId: string | null | undefined } | null;
	turns: Map<string, TurnCacheEntry>;
}

export function createTurnCache(): TurnCache {
	return { scope: null, turns: new Map() };
}

/** Plain rows only: a work log's derived fields are not worth comparing. */
function sameItem(a: DisplayItem, b: DisplayItem): boolean {
	if (a.kind === 'message' && b.kind === 'message') return a.message === b.message && a.index === b.index;
	if (a.kind === 'system_group' && b.kind === 'system_group') {
		return a.indices.length === b.indices.length
			&& a.indices.every((index, i) => index === b.indices[i] && a.messages[i] === b.messages[i]);
	}
	return false;
}

/** Raw index a plain row item starts at; null for work logs (never reused). */
function plainItemIndex(item: DisplayItem): number | null {
	if (item.kind === 'message') return item.index;
	if (item.kind === 'system_group') return item.indices[0] ?? null;
	return null;
}

function sameRows(a: Positioned[], b: Positioned[]): boolean {
	if (a.length !== b.length) return false;
	for (let i = 0; i < a.length; i++) {
		if (a[i].message !== b[i].message || a[i].index !== b[i].index) return false;
	}
	return true;
}

export function buildTranscriptModel(
	messages: ChatMessage[],
	options: BuildOptions,
	cache?: TurnCache
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
	const midTurnUserIds = new Set<string>();
	for (const turn of turns) {
		const openingTurnId = turn[0]?.message.turnId;
		if (!openingTurnId) continue;
		for (const { message } of turn.slice(1)) {
			if (message.id && message.turnId === openingTurnId && isNavigableUserTurn(message)) midTurnUserIds.add(message.id);
		}
	}

	const scope = cache?.scope;
	const reusable = !!cache && !!scope
		&& scope.groupingEnabled === groupingEnabled && scope.sessionId === options.sessionId;
	const previousTurns = reusable ? cache.turns : null;
	const nextTurns = new Map<string, TurnCacheEntry>();
	turns.forEach((turn, turnIndex) => {
		const key = turnKeyOf(turn);
		const runningToolIds = options.runningToolIds;
		const unsettled = groupingEnabled && (
			(latestTurnUnsettled && turnIndex === turns.length - 1) ||
			!!options.heldTurnKeys?.has(key) ||
			(!!runningToolIds?.size && turn.some((entry) => !!entry.message.toolId && runningToolIds.has(entry.message.toolId))));
		const hit = previousTurns?.get(key);
		if (hit && hit.unsettled === unsettled && sameRows(hit.rows, turn)) {
			for (const item of hit.items) items.push(item);
			nextTurns.set(key, hit);
			return;
		}
		const start = items.length;
		emitTurn(turn, unsettled);
		// A rebuilt turn keeps the identity of plain rows that did not change
		// (its opening prompt, mostly), so only its work log re-renders.
		if (hit) {
			// Index the prior plain items once by their first raw index: an
			// ungrouped long turn emits one item per row, and scanning the prior
			// list per row made each append quadratic in the turn's length.
			let prior: Map<number, DisplayItem> | undefined;
			for (let i = start; i < items.length; i++) {
				const fresh = items[i];
				const at = plainItemIndex(fresh);
				if (at === null) continue;
				if (!prior) {
					prior = new Map();
					for (const item of hit.items) {
						const priorAt = plainItemIndex(item);
						if (priorAt !== null && !prior.has(priorAt)) prior.set(priorAt, item);
					}
				}
				const same = prior.get(at);
				if (same && sameItem(same, fresh)) items[i] = same;
			}
		}
		if (cache) nextTurns.set(key, { rows: turn, unsettled, items: items.slice(start) });
	});
	if (cache) {
		cache.scope = { groupingEnabled, sessionId: options.sessionId };
		cache.turns = nextTurns;
	}

	function emitTurn(turn: Positioned[], unsettledTurn: boolean): void {
		if (!groupingEnabled) {
			emitWithSystemGroups(turn, items);
			return;
		}

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

		// One turn, one fold. A steer does not end the turn, so it does not cut the
		// settled presentation either: it stays a visible row in time order inside
		// the turn's sequence, and the fold label and summary describe the whole turn.
		const turnMessages = turn.map((entry) => entry.message);
		const durationLabel = turnFoldLabel(turnMessages, turnWasInterrupted(turnMessages));
		const prefix = userIndex < 0 ? [] : turn.slice(0, 1);
		const response = turn.slice(prefix.length);
		const isSteer = (entry: Positioned) => entry.message.role === 'user' && !agentDelivery(entry.message);
		// The fold renders its gallery above the rows it keeps, so only media from
		// before the first steer may join it; later media stays in its own rows,
		// in time order below the steer.
		const firstSteer = response.findIndex(isSteer);
		const galleryRows = firstSteer < 0 ? response : response.slice(0, firstSteer);
		const gallery = galleryMediaForTurn(galleryRows.map((entry) => entry.message), options.sessionId);
		const galleryEntries = new Set(gallery.length > 1 ? galleryRows : []);
		const terminalAssistants = terminalProse(response);
		// Prose followed by a question tool before the next prose or steer is
		// the question's lead-in: it asks too, so it stays.
		const askingProse = askingLeadIns(response);
		// A compaction card the turn worked past is mid-turn; one trailing the
		// answer (an idle or manual /compact) keeps its own row.
		const lastWork = response.findLastIndex((entry) => isTurnWork(entry.message));
		const midTurnCompactions = new Set(
			response.filter((entry, i) => i < lastWork && isCompactionCard(entry.message))
		);
		const shouldHide = (entry: Positioned) =>
			!isSteer(entry) && (
				isCompletedRoutineWork(entry.message, galleryEntries.has(entry)) ||
				isGroupableSystem(entry.message) ||
				midTurnCompactions.has(entry) ||
				entry.message.type === 'tool_result' ||
				(isAssistantText(entry.message) &&
					!entry.message.streaming &&
					!terminalAssistants.has(entry) &&
					!askingProse.has(entry) &&
					!isProtectedProse(entry.message.content))
			);
		const folded = response
			.filter(shouldHide)
			.map((entry) => ({ message: entry.message, index: entry.index }));

		// A turn with nothing to fold still keeps its group: the live turn
		// rendered inside it, so settling must not unmount a worker card or a
		// report the reader expanded. WorkLogGroup draws no disclosure for it.
		if (response.length === 0) {
			emitWithSystemGroups(turn, items);
			return;
		}

		// The opening prompt stays outside the disclosure; steers ride inside the
		// sequence as protected rows (WorkLogGroup re-emits their turn anchors).
		emitWithSystemGroups(prefix, items);
		// Keyed by the prompt that opens the turn, which never changes. Keying by
		// the first folded row remounted the group (collapsing an open log and
		// replaying its settle animation) whenever a late tool result or a
		// finished delegation folded an earlier row. The live turn uses the same
		// key, so settling flips this group's mode instead of mounting a new one.
		items.push({
			kind: 'work_log',
			...workLogKeys(prefix[0] ?? response[0]),
			gallery,
			mode: 'settled',
			folded,
			tail: [],
			// A hidden code-mode cell draws nothing, as in the live turn. Kept in
			// the sequence it would make a turn that folds nothing look foldable,
			// so settling drew a "Show work details" button that revealed nothing.
			sequence: response.filter((entry) => !isHiddenCodeModeCell(entry.message)).map((entry) => ({
				message: entry.message,
				index: entry.index,
				hiddenWhenCollapsed: shouldHide(entry),
				...(galleryEntries.has(entry) ? { inGallery: true } : {})
			})),
			durationLabel,
			summary: summarizeWork(folded),
			compactSummary: summarizeWorkCompact(folded)
		});
	}

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
		latestUserTurnId,
		midTurnUserIds
	};
}
