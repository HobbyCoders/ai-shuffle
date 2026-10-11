/**
 * One transcript row per hand-off.
 *
 * A hand-off leaves several records in the chat (see pending-handoff.ts for
 * the full list): the agent's HandOffSession call (or the owner's `queued`
 * row from "Move when this turn ends"), then `handoff` rows recorded by
 * app/core/session_handoff.py as it runs — `started` when the commit, push and
 * move begin, and one of `moved`, `failed`, `timeout` or `cancelled` when it
 * ends. After the move the new worker may add `held_failed` or
 * `resume_failed` about the messages it could not take.
 *
 * Shown as they are, that is two to four rows for one action. This folds them
 * into ONE episode anchored at the first record, the way task patches fold
 * into one worker card: the anchor row renders the episode's current phase,
 * and every later record of the same hand-off is absorbed (not rendered).
 *
 * Identity, not proximity: the backend allows one queued or running hand-off
 * per chat at a time (`request` and `queue_move` refuse a second one), so a
 * record belongs to the episode that is open when it arrives, and its worker
 * must match. Rows without any open episode start their own (an anchor that is
 * not loaded, a worker restart that resumed a move). The rows carry no
 * hand-off id yet; when they do, matching on it replaces the worker check.
 */
import type { ChatMessage } from '$lib/stores/types';

export type HandoffPhase =
	/** Queued: waits for the turn, the queued messages and background work to end. */
	| 'starting'
	/** Commit, push, sync and move are running. */
	| 'sending'
	/** Settled: the chat now runs on the other worker. */
	| 'done'
	/** Settled: the move failed or timed out; the chat stays here. */
	| 'failed'
	/** Settled: cancelled before it started (by the agent, the owner or a restart). */
	| 'cancelled'
	/** Settled: the tool refused the request; nothing was queued. */
	| 'refused';

export interface HandoffEpisode {
	/** Stable key: the anchor row's id. */
	key: string;
	/** The target worker's label ('' only when nothing named one). */
	worker: string;
	/** Who asked: the agent's HandOffSession or the owner's move. */
	by: 'agent' | 'owner';
	phase: HandoffPhase;
	/** The HandOffSession call itself is still running (no result yet). */
	requesting?: boolean;
	/** Queued messages run before the move (owner's move, #1819). */
	afterQueued?: boolean;
	/** The agent's note for the new worker. */
	note?: string;
	/** The backend's sentence for a settled episode. */
	outcome?: string;
	/** Machine reason on a settled episode (`open_prompt`, `restart`, …). */
	reason?: string;
	/** Later notes from the new worker (messages it could not take). */
	followUps: string[];
	/** The move itself began (`started`): a failure after this broke mid-send. */
	sent?: boolean;
	/** Number of records folded into this row, the anchor included. */
	records: number;
}

export interface HandoffEpisodes {
	/** The episode each anchor row renders. */
	byAnchor: ReadonlyMap<ChatMessage, HandoffEpisode>;
	/** Rows folded into an episode: they draw nothing of their own. */
	absorbed: ReadonlySet<ChatMessage>;
	/** The hand-off still waiting or running, if any (the composer chip's source). */
	open: HandoffEpisode | null;
}

const HAND_OFF_TOOL = /(^|__)HandOffSession$/;
const SETTLED: ReadonlySet<HandoffPhase> = new Set(['done', 'failed', 'cancelled', 'refused']);

export const EMPTY_HANDOFFS: HandoffEpisodes = { byAnchor: new Map(), absorbed: new Set(), open: null };

export function isHandoffTool(message: ChatMessage): boolean {
	return message.type === 'tool_use' && HAND_OFF_TOOL.test(message.toolName ?? '');
}

export function isHandoffRow(message: ChatMessage): boolean {
	return message.systemSubtype === 'handoff';
}

export function isSettledPhase(phase: HandoffPhase): boolean {
	return SETTLED.has(phase);
}

/** The tool's result as an object (it may arrive as JSON text). */
export function handoffResult(result: unknown): Record<string, unknown> | null {
	if (result && typeof result === 'object') return result as Record<string, unknown>;
	if (typeof result !== 'string') return null;
	const text = result.trim();
	if (!text.startsWith('{')) return null;
	try {
		const parsed: unknown = JSON.parse(text);
		return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
	} catch {
		return null;
	}
}

const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '');

function rowData(message: ChatMessage): Record<string, unknown> {
	return (message.systemData ?? {}) as Record<string, unknown>;
}

function anchorKey(message: ChatMessage, index: number): string {
	return message.serverId ?? message.id ?? `handoff-${index}`;
}

/** Read every hand-off in `messages` as one episode per hand-off. */
export function handoffEpisodes(messages: readonly ChatMessage[]): HandoffEpisodes {
	let byAnchor: Map<ChatMessage, HandoffEpisode> | null = null;
	let absorbed: Set<ChatMessage> | null = null;
	/** The episode still waiting or running; null once it settled. */
	let open: HandoffEpisode | null = null;
	/** The latest episode, open or settled: the new worker's notes attach to it. */
	let last: HandoffEpisode | null = null;

	const start = (anchor: ChatMessage, index: number, episode: Omit<HandoffEpisode, 'key' | 'followUps' | 'records'>) => {
		const made: HandoffEpisode = { key: anchorKey(anchor, index), followUps: [], records: 1, ...episode };
		(byAnchor ??= new Map()).set(anchor, made);
		last = made;
		open = isSettledPhase(made.phase) ? null : made;
		return made;
	};
	const absorb = (message: ChatMessage, into: HandoffEpisode, patch: Partial<HandoffEpisode>) => {
		(absorbed ??= new Set()).add(message);
		Object.assign(into, patch, { records: into.records + 1 });
		if (isSettledPhase(into.phase)) open = null;
	};
	/** The open episode this record belongs to: same worker, or the record names none. */
	const owning = (worker: string): HandoffEpisode | null => {
		const current = open as HandoffEpisode | null;
		if (!current) return null;
		return !worker || !current.worker || current.worker === worker ? current : null;
	};

	messages.forEach((message, index) => {
		if (isHandoffTool(message)) {
			const input = (message.toolInput ?? {}) as { worker?: unknown; note?: unknown; cancel?: unknown };
			const asked = text(input.worker);
			const note = text(input.note) || undefined;
			if (message.toolStatus === 'error') {
				// A refused call changed nothing: an open hand-off still stands, and the
				// refusal is its own settled row so the reason stays readable.
				const [stillOpen, latest] = [open, last];
				start(message, index, {
					worker: asked, by: 'agent', phase: 'refused',
					outcome: text(message.toolResult) || undefined
				});
				open = stillOpen;
				last = latest ?? last;
				return;
			}
			const result = handoffResult(message.toolResult);
			if (input.cancel || (result && 'cancelled' in result)) {
				const current = open as HandoffEpisode | null;
				if (current && (!result || result.cancelled !== false)) {
					absorb(message, current, { phase: 'cancelled', reason: 'agent' });
				} else if (!current) {
					start(message, index, {
						worker: asked, by: 'agent', phase: 'cancelled', reason: 'agent',
						requesting: !result && message.toolStatus !== 'complete' ? true : undefined
					});
				} else {
					// `cancelled: false`: the move had already started; it goes on.
					(absorbed ??= new Set()).add(message);
					current.records++;
				}
				return;
			}
			const worker = text(result?.worker) || asked;
			const current = owning(worker);
			if (current) {
				// The same move asked again: the queued one stands.
				absorb(message, current, note && !current.note ? { note } : {});
				return;
			}
			start(message, index, {
				worker, by: 'agent', phase: 'starting', note,
				requesting: result?.queued === true ? undefined : true
			});
			return;
		}

		if (!isHandoffRow(message)) return;
		const data = rowData(message);
		const status = text(data.status);
		const worker = text(data.worker);
		const by = data.by === 'owner' ? 'owner' : undefined;
		const outcome = text(data.content) || text(message.content) || undefined;
		const reason = text(data.reason) || undefined;

		switch (status) {
			case 'queued': {
				const current = owning(worker);
				const afterQueued = data.after === 'queued' ? true : undefined;
				if (current) {
					absorb(message, current, { phase: 'starting', afterQueued, by: by ?? current.by });
					return;
				}
				start(message, index, { worker, by: by ?? 'owner', phase: 'starting', afterQueued });
				return;
			}
			case 'started': {
				const note = text(data.note) || undefined;
				const current = owning(worker);
				if (current) {
					absorb(message, current, {
						phase: 'sending', sent: true, requesting: undefined, worker: worker || current.worker,
						...(note ? { note } : {})
					});
					return;
				}
				start(message, index, { worker, by: by ?? 'agent', phase: 'sending', sent: true, note });
				return;
			}
			case 'moved':
			case 'failed':
			case 'timeout':
			case 'cancelled': {
				const phase: HandoffPhase =
					status === 'moved' ? 'done'
					: status === 'cancelled' || (status === 'failed' && reason === 'open_prompt') ? 'cancelled'
					: 'failed';
				// `moved` may name the worker that really took the chat, which can
				// differ from the target when a lost reply hid a finished move.
				const current = status === 'moved' ? (open as HandoffEpisode | null) : owning(worker);
				const patch: Partial<HandoffEpisode> = {
					phase, outcome, requesting: undefined,
					reason: status === 'timeout' ? 'timeout' : reason,
					...(worker ? { worker } : {})
				};
				if (current) {
					absorb(message, current, patch);
					return;
				}
				// "No move is queued" with nothing open closes a stale row we never saw.
				if (status === 'cancelled' && reason === 'nothing_queued') {
					(absorbed ??= new Set()).add(message);
					return;
				}
				start(message, index, {
					worker, by: by ?? 'agent', phase, outcome,
					reason: status === 'timeout' ? 'timeout' : reason
				});
				return;
			}
			case 'held_failed':
			case 'resume_failed': {
				const into = (open as HandoffEpisode | null) ?? (last as HandoffEpisode | null);
				if (!into || !outcome) return;
				(absorbed ??= new Set()).add(message);
				into.followUps = [...into.followUps, outcome];
				into.records++;
				return;
			}
			default:
				return;
		}
	});

	if (!byAnchor && !absorbed) return EMPTY_HANDOFFS;
	return { byAnchor: byAnchor ?? new Map(), absorbed: absorbed ?? new Set(), open };
}

/** Same anchors rendering the same episodes (by value): a rebuild changed nothing here. */
export function sameHandoffs(a: HandoffEpisodes, b: HandoffEpisodes): boolean {
	if (a === b) return true;
	if (a.byAnchor.size !== b.byAnchor.size || a.absorbed.size !== b.absorbed.size) return false;
	for (const [anchor, episode] of b.byAnchor) {
		const before = a.byAnchor.get(anchor);
		if (!before || JSON.stringify(before) !== JSON.stringify(episode)) return false;
	}
	for (const row of b.absorbed) if (!a.absorbed.has(row)) return false;
	return true;
}
