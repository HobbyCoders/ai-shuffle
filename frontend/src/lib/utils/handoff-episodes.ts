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
 * not loaded, a worker restart that resumed a move). Records that carry a
 * `handoff_id` are grouped strictly by it; without one, a late `cancelled`
 * settles the move a newer one replaced, never the newer move. On the source
 * worker the move settles when the chat's owner becomes the target
 * (`settleByOwner`): no source-side transcript record exists for success.
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
	/** The backend's hand-off id, once its records carry one. */
	id?: string;
	/** The target worker's device id, when a record names it. */
	targetId?: string;
	/** A cancel call is on its way; the move stands until it is confirmed. */
	cancelling?: boolean;
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

/** The hand-off id a record carries, once the backend sends one. */
function idOf(value: Record<string, unknown> | null | undefined): string | undefined {
	return text(value?.handoff_id) || undefined;
}

/** The HandOffSession result says the same move was asked for again. */
function isRepeat(result: Record<string, unknown> | null): boolean {
	return /already queued/i.test(text(result?.message));
}

/** Read every hand-off in `messages` as one episode per hand-off. */
export function handoffEpisodes(messages: readonly ChatMessage[]): HandoffEpisodes {
	let byAnchor: Map<ChatMessage, HandoffEpisode> | null = null;
	let absorbed: Set<ChatMessage> | null = null;
	/** Episodes still waiting or running, oldest first (two only in a cancel race). */
	let open: HandoffEpisode[] = [];
	/** The latest episode, open or settled: the new worker's notes attach to it. */
	let last: HandoffEpisode | null = null;
	const byId = new Map<string, HandoffEpisode>();
	/**
	 * Moves a newer move replaced before their own `cancelled` record arrived.
	 * `cancel_move` drops the queued move before it records the cancel, so a new
	 * `queued` can land first; that late record settles the older move, never
	 * the newer one. Only needed while records carry no id.
	 */
	let replaced: HandoffEpisode[] = [];
	/**
	 * Moves without an id that a confirmed HandOffSession cancel already
	 * settled. The call sits before the `cancelled` record it causes, so that
	 * record is the same cancel, confirmed: it joins this row instead of opening
	 * a second one. Taken before the open move, like a replaced move's late
	 * record, so it never settles a newer move.
	 */
	let cancelledByCall: HandoffEpisode[] = [];

	const absorbOnly = (message: ChatMessage, into: HandoffEpisode) => {
		(absorbed ??= new Set()).add(message);
		into.records++;
	};
	const settle = (episode: HandoffEpisode) => {
		open = open.filter((e) => e !== episode);
	};
	const start = (anchor: ChatMessage, index: number, episode: Omit<HandoffEpisode, 'key' | 'followUps' | 'records'>) => {
		const made: HandoffEpisode = { key: anchorKey(anchor, index), followUps: [], records: 1, ...episode };
		(byAnchor ??= new Map()).set(anchor, made);
		if (made.id) byId.set(made.id, made);
		if (made.phase !== 'refused') last = made;
		if (!isSettledPhase(made.phase)) {
			// The backend runs one move at a time: a new one means the open one
			// for the same chat was dropped, even if its record is still to come.
			for (const older of open) {
				Object.assign(older, { phase: 'cancelled', reason: 'replaced', requesting: undefined, cancelling: undefined });
				if (!older.id) replaced.push(older);
			}
			open = [made];
		}
		return made;
	};
	const absorb = (message: ChatMessage, into: HandoffEpisode, patch: Partial<HandoffEpisode>) => {
		absorbOnly(message, into);
		Object.assign(into, patch);
		if (into.id) byId.set(into.id, into);
		if (isSettledPhase(into.phase)) settle(into);
	};
	const matches = (episode: HandoffEpisode, worker: string) =>
		!worker || !episode.worker || episode.worker === worker;
	/**
	 * The episode a record belongs to. With an id, only that episode. Without
	 * one, the newest open move for the same worker; a late `cancelled` first
	 * settles a move a newer one replaced.
	 */
	const owning = (id: string | undefined, worker: string, lateCancel = false): HandoffEpisode | null => {
		// An explicit id matches only the episode with that id: a record whose
		// anchor is not loaded stands on its own, never in another move's row.
		if (id) return byId.get(id) ?? null;
		if (lateCancel) {
			const stale = replaced.find((e) => matches(e, worker));
			if (stale) {
				replaced = replaced.filter((e) => e !== stale);
				return stale;
			}
			const confirmed = cancelledByCall.find((e) => matches(e, worker));
			if (confirmed) {
				cancelledByCall = cancelledByCall.filter((e) => e !== confirmed);
				return confirmed;
			}
		}
		const current = open.at(-1);
		return current && matches(current, worker) ? current : null;
	};

	messages.forEach((message, index) => {
		if (isHandoffTool(message)) {
			const input = (message.toolInput ?? {}) as { worker?: unknown; note?: unknown; cancel?: unknown };
			const asked = text(input.worker);
			const note = text(input.note) || undefined;
			if (message.toolStatus === 'error') {
				// A refused call changed nothing: an open hand-off still stands, and the
				// refusal is its own settled row so the reason stays readable.
				const stillOpen = open;
				start(message, index, {
					worker: asked, by: 'agent', phase: 'refused',
					outcome: text(message.toolResult) || undefined
				});
				open = stillOpen;
				return;
			}
			const result = handoffResult(message.toolResult);
			const id = idOf(result);
			if (input.cancel || (result && 'cancelled' in result)) {
				const current = (id ? byId.get(id) : open.at(-1)) ?? null;
				const confirmed = result?.cancelled === true;
				if (current && !isSettledPhase(current.phase)) {
					// Only a confirmed cancel settles the move. Until the call returns
					// it is "cancelling"; `cancelled: false` means it had already started.
					if (confirmed) {
						absorb(message, current, { phase: 'cancelled', reason: 'agent', cancelling: undefined });
						if (!current.id) cancelledByCall.push(current);
					} else absorb(message, current, { cancelling: result ? undefined : true });
				} else if (confirmed) {
					const made = start(message, index, { worker: asked, by: 'agent', phase: 'cancelled', reason: 'agent', id });
					if (!made.id) cancelledByCall.push(made);
				}
				// An unresolved or refused cancel with nothing open renders as the call itself.
				return;
			}
			const worker = text(result?.worker) || asked;
			const current = id ? owning(id, worker) : (isRepeat(result) || !result ? owning(undefined, worker) : null);
			if (current && !isSettledPhase(current.phase)) {
				// The same move asked again (or its result still on the way): the queued one stands.
				absorb(message, current, { ...(note && !current.note ? { note } : {}), ...(id ? { id } : {}) });
				return;
			}
			start(message, index, {
				worker, by: 'agent', phase: 'starting', note, id,
				requesting: result?.queued === true ? undefined : true
			});
			return;
		}

		if (!isHandoffRow(message)) return;
		const data = rowData(message);
		const status = text(data.status);
		const worker = text(data.worker);
		const id = idOf(data);
		const by = data.by === 'owner' ? 'owner' : undefined;
		const outcome = text(data.content) || text(message.content) || undefined;
		const reason = text(data.reason) || undefined;
		const targetId = text(data.target_id) || undefined;

		switch (status) {
			case 'queued': {
				// The owner's move API refuses a second move, so a `queued` row is a
				// new move unless it carries the open move's id.
				const afterQueued = data.after === 'queued' ? true : undefined;
				const current = id ? byId.get(id) : undefined;
				if (current) {
					absorb(message, current, { phase: 'starting', afterQueued, by: by ?? current.by });
					return;
				}
				start(message, index, { worker, by: by ?? 'owner', phase: 'starting', afterQueued, id, targetId });
				return;
			}
			case 'started': {
				const note = text(data.note) || undefined;
				const current = owning(id, worker);
				if (current) {
					absorb(message, current, {
						phase: 'sending', sent: true, requesting: undefined, worker: worker || current.worker,
						...(note ? { note } : {}), ...(id ? { id } : {}), ...(targetId ? { targetId } : {})
					});
					return;
				}
				start(message, index, { worker, by: by ?? 'agent', phase: 'sending', sent: true, note, id, targetId });
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
				const current = owning(id, phase === 'done' ? '' : worker, status === 'cancelled');
				const patch: Partial<HandoffEpisode> = {
					phase, outcome, requesting: undefined, cancelling: undefined,
					reason: status === 'timeout' ? 'timeout' : reason,
					...(worker ? { worker } : {}), ...(id ? { id } : {})
				};
				if (current) {
					// The record confirming a cancel the call already settled keeps
					// the call's reason when it names none.
					if (current.phase === 'cancelled' && phase === 'cancelled' && !patch.reason) patch.reason = current.reason;
					absorb(message, current, patch);
					return;
				}
				// "No move is queued" with nothing open closes a stale row we never saw.
				if (status === 'cancelled' && reason === 'nothing_queued') {
					(absorbed ??= new Set()).add(message);
					return;
				}
				const stillOpen = open;
				start(message, index, {
					worker, by: by ?? 'agent', phase, outcome, id,
					reason: status === 'timeout' ? 'timeout' : reason
				});
				open = stillOpen;
				return;
			}
			case 'held_failed':
			case 'resume_failed': {
				const into = id ? byId.get(id) : open.at(-1) || (last as HandoffEpisode | null);
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
	return { byAnchor: byAnchor ?? new Map(), absorbed: absorbed ?? new Set(), open: open.at(-1) ?? null };
}

/**
 * Who runs the chat now, from its session row (the sidebar's "on DesktopWorker").
 * The source worker's transcript never sees `moved` (the new worker records
 * it), so a confirmed change of owner to the target is what settles a move
 * there.
 */
export interface HandoffOwner {
	deviceId?: string | null;
	deviceName?: string | null;
}

const settledByOwner = new WeakMap<HandoffEpisodes, HandoffEpisodes>();
/** Owner seen when each sending move was first observed, by session and episode. */
const ownerAtSend = new Map<string, Seen>();

/** An observed owner: a stable device id and/or a display name. */
interface Seen {
	id: string;
	name: string;
}
/** Moves whose owner was seen to change to their target. */
const ownerMoved = new Set<string>();

function ownedByTarget(episode: HandoffEpisode, owner: HandoffOwner): boolean {
	if (episode.targetId && owner.deviceId) return episode.targetId === owner.deviceId;
	const name = text(owner.deviceName).toLowerCase();
	return !!name && !!episode.worker && name === episode.worker.toLowerCase();
}

function seenOf(owner: HandoffOwner): Seen {
	return { id: text(owner.deviceId), name: text(owner.deviceName).toLowerCase() };
}

/**
 * Whether `now` is a different owner than `before`. The device id is the
 * identity: when both have one, only a different id is a transfer. A display
 * name decides only when an id is missing on either side, and only when both
 * names are known. Filling in a missing id or name, or renaming the same id,
 * is the same owner described better, not a transfer.
 */
function ownerChanged(before: Seen, now: Seen): boolean {
	if (before.id && now.id) return before.id !== now.id;
	return !!before.name && !!now.name && before.name !== now.name;
}

/**
 * What the owning worker says is pending (`GET /sessions/{id}/handoff`).
 * `known: false` until it has been read for this session.
 */
export interface HandoffPendingView {
	known: boolean;
	pending: { handoff_id?: string | null; target_id?: string | null; moving?: boolean } | null;
	/**
	 * When the read was sent (`beginHandoffPendingRead`). It counts for a move
	 * only if it was sent after the page first saw that move: an answer from
	 * before says nothing about it.
	 */
	seq?: number;
}

/** Reads of `GET /handoff` sent so far, page-wide. */
let pendingReads = 0;

/** Number a pending read before it is sent; the answer is recorded with it. */
export function beginHandoffPendingRead(): number {
	return ++pendingReads;
}

/** Reads sent before each open move was first seen, by session and episode. */
const readsAtFirstSight = new Map<string, number>();

/** Target device of each identified move, learned from the pending move while it was pending. */
const targetById = new Map<string, string>();

/** Remember a pending move's target, so its end can be recognised after it stops being pending. */
export function noteHandoffTarget(handoffId: string | null | undefined, targetId: string | null | undefined): void {
	const id = text(handoffId);
	const target = text(targetId);
	if (id && target) targetById.set(id, target);
}

/**
 * Settle a sending move on the source worker. No transcript record says it
 * succeeded there: the new worker writes `moved`, and the source only updates
 * the session (`owner_device_id` becomes the target device, #2308).
 *
 * - An identified move (`handoff_id` X) whose target device is known is done
 *   when the session's `owner_device_id` IS that target AND the owning worker
 *   no longer reports X pending (`pending` null or another id), which also
 *   holds on a reload after the move; or when the owner was SEEN to change to
 *   the target while the page watched. Owner equality with X still pending
 *   (or with pending not yet read) proves nothing.
 * - If X's target device is not known (a reload after the move: the pending
 *   move, the only source of `target_id`, is gone) the same authoritative pair
 *   is checked against the target's name instead.
 * - A move without an id (older workers) settles only on an observed change.
 *
 * Observed owners are kept per session, so the row, the chip and the composer
 * agree. Returns `handoffs` itself when nothing changes, and the same result
 * for the same inputs, so rows keep identity.
 */
export function settleByOwner(
	handoffs: HandoffEpisodes,
	owner: HandoffOwner | null | undefined,
	sessionId: string | null | undefined = '',
	pendingView?: HandoffPendingView | null
): HandoffEpisodes {
	const current = handoffs.open;
	if (!current) return handoffs;
	const slot = `${sessionId ?? ''}\0${current.key}`;
	if (!readsAtFirstSight.has(slot)) readsAtFirstSight.set(slot, pendingReads);
	if (current.phase !== 'sending') return handoffs;
	if (!ownerMoved.has(slot) && !decide(current, slot, owner, pendingView)) return handoffs;
	const cached = settledByOwner.get(handoffs);
	if (cached) return cached;
	const done: HandoffEpisode = { ...current, phase: 'done', cancelling: undefined };
	const byAnchor = new Map(handoffs.byAnchor);
	for (const [anchor, episode] of byAnchor) if (episode === current) byAnchor.set(anchor, done);
	const value: HandoffEpisodes = { byAnchor, absorbed: handoffs.absorbed, open: null };
	settledByOwner.set(handoffs, value);
	return value;
}

/** Whether the sending move `current` has now ended on this side (see settleByOwner). */
function decide(
	current: HandoffEpisode,
	slot: string,
	owner: HandoffOwner | null | undefined,
	pendingView: HandoffPendingView | null | undefined
): boolean {
	// Only a read sent after this move was first seen answers for it.
	const fresh = !!pendingView?.known && (pendingView.seq ?? 0) > (readsAtFirstSight.get(slot) ?? Infinity);
	const pending = fresh ? (pendingView?.pending ?? null) : null;
	if (current.id && pending && text(pending.handoff_id) === current.id) noteHandoffTarget(current.id, pending.target_id);
	if (!owner || (!owner.deviceId && !text(owner.deviceName))) return false;
	const target = current.targetId || (current.id ? targetById.get(current.id) : undefined);
	const ownedNow = target ? !!owner.deviceId && owner.deviceId === target : ownedByTarget(current, owner);
	// The authoritative pair: owned by the target, and X is no longer pending there.
	// Not kept: a newer read reporting X pending again takes it back.
	const noLongerPending = !!current.id && fresh && text(pending?.handoff_id) !== current.id;
	if (ownedNow && noLongerPending) return true;
	// Otherwise only a change of owner the page saw happen.
	const now = seenOf(owner);
	const seen = ownerAtSend.get(slot);
	if (seen === undefined) {
		ownerAtSend.set(slot, now);
		return false;
	}
	if (!ownerChanged(seen, now)) {
		// The same owner, maybe described better: keep what is now known.
		ownerAtSend.set(slot, { id: seen.id || now.id, name: now.name || seen.name });
		return false;
	}
	// A change the page saw happen is kept.
	if (ownedNow) ownerMoved.add(slot);
	return ownedNow;
}

/** Test hook: forget every observed owner. */
export function resetOwnerTransitions(): void {
	ownerAtSend.clear();
	targetById.clear();
	ownerMoved.clear();
	readsAtFirstSight.clear();
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
