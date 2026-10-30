/** Bounded, installation-local projections of the authoritative V2 fold.
 * No serving call site uses this yet. Storage owns generation/CAS/queueing;
 * this module alone owns message semantics and detached-row materialization.
 */
import type { ChatMessage } from '../../types';
import {
	dismissCapturedProse, initialState, parseWireEvent, reduceWithMessageEffects,
	rewindCapturedMessage, sealState, withLogEpoch,
	type CapturedMessageEffect, type V2SessionState, type WireEvent
} from './reducer';
import { packProjection, unpackProjection } from './transcript-codec';

export const TRANSCRIPT_PROJECTION_VERSION = 1;
export const PROJECTION_LIMITS = {
	events: 128, referencedRows: 128, referenceKeys: 512, effects: 256,
	marginRows: 96, carryBytes: 4 * 1024 * 1024, rowBytes: 512 * 1024,
	inputBytes: 12 * 1024 * 1024
} as const;

export class ProjectionUnsupported extends Error {
	constructor(readonly code: string) { super(code); this.name = 'ProjectionUnsupported'; }
}

interface Slot { order: number; originOrder: number; terminal: boolean }
interface CapturedRow extends Slot {
	message: ChatMessage;
	origin: number;
	foldedThrough: number;
}
interface Carry {
	version: number;
	state: V2SessionState;
	slots: Map<string, Slot>;
	originOrders: Map<string, number>;
	nextOrder: number;
	nextOriginOrder: number;
	effects: CapturedMessageEffect[];
}
export interface ProjectionRow {
	id: string;
	order: number;
	origin: number;
	foldedThrough: number;
	aliases: string[];
	body: string;
}
export interface ProjectionDelta {
	carry: string;
	upserts: ProjectionRow[];
	removed: string[];
	lastSeq: number;
	effectCount: number;
}
export interface ProjectionFold {
	/** Last authoritative attempt boundary: safe stable/raw-tail split. */
	boundary: ProjectionDelta | null;
	/** Complete bounded evaluation, never sealed speculatively. */
	preview: ProjectionDelta;
}

const byteSize = (value: string): number => new TextEncoder().encode(value).byteLength;
const encode = (value: unknown): string => JSON.stringify(packProjection(value));
const decode = (value: string): unknown => unpackProjection(JSON.parse(value));

function emptyCarry(): Carry {
	return { version: TRANSCRIPT_PROJECTION_VERSION, state: initialState(), slots: new Map(),
		originOrders: new Map(), nextOrder: 0, nextOriginOrder: 0, effects: [] };
}
function readCarry(body: string | null): Carry {
	if (body === null) return emptyCarry();
	if (byteSize(body) > PROJECTION_LIMITS.carryBytes) throw new ProjectionUnsupported('carry_limit');
	const value = decode(body) as Carry;
	if (value?.version !== TRANSCRIPT_PROJECTION_VERSION || !Array.isArray(value.state?.messages) ||
		!(value.state.messageOriginSeq instanceof Map) || !(value.slots instanceof Map) ||
		!(value.originOrders instanceof Map) || !Array.isArray(value.effects) ||
		value.effects.length > PROJECTION_LIMITS.effects || value.state.logEpoch !== null ||
		!Number.isSafeInteger(value.nextOrder) || !Number.isSafeInteger(value.nextOriginOrder)) {
		throw new ProjectionUnsupported('carry_shape');
	}
	return value;
}
function frames(values: unknown[]): WireEvent[] {
	if (values.length > PROJECTION_LIMITS.events) throw new ProjectionUnsupported('event_limit');
	return values.map(value => {
		const frame = parseWireEvent(value);
		if (!frame) throw new ProjectionUnsupported('wire_envelope');
		return frame;
	});
}

function messageAliases(message: ChatMessage): string[] {
	const aliases = new Set<string>();
	const record = message as unknown as Record<string, unknown>;
	for (const key of ['id', 'serverId', 'jsonlUuid', 'toolId', 'agentSessionId',
		'agentLaunchToolUseId', 'commandId', 'queuedTurnId']) {
		const value = record[key];
		if (typeof value === 'string' && value) aliases.add(value);
	}
	for (const child of message.agentChildren ?? []) {
		const row = child as unknown as Record<string, unknown>;
		for (const key of ['id', 'toolId', 'toolUseId']) {
			if (typeof row[key] === 'string' && row[key]) aliases.add(row[key] as string);
		}
	}
	return [...aliases];
}

/** Resolve only identity fields, never arbitrary tool inputs/results as IDs. */
export function projectionReferences(body: string | null, values: unknown[]): string[] {
	const carry = readCarry(body);
	const present = new Set(carry.state.messages.flatMap(messageAliases));
	const keys = new Set<string>();
	for (const frame of frames(values)) {
		for (const object of [frame, frame.data]) {
			if (!object || typeof object !== 'object' || Array.isArray(object)) continue;
			for (const [key, value] of Object.entries(object)) {
				if (['session_id', 'run_id', 'turn_id', 'profile_id', 'device_id'].includes(key)) continue;
				if (/(?:id|uuid)$/i.test(key) && typeof value === 'string' && value && !present.has(value)) {
					keys.add(value);
				}
			}
		}
	}
	if (keys.size > PROJECTION_LIMITS.referenceKeys) throw new ProjectionUnsupported('reference_limit');
	return [...keys];
}

function applyEffects(row: CapturedRow, effects: CapturedMessageEffect[]): CapturedRow | null {
	let message: ChatMessage | null = row.message;
	for (const effect of effects) {
		if (effect.seq <= row.foldedThrough) continue;
		message = effect.kind === 'dismiss'
			? dismissCapturedProse(message, row.origin, effect.through, effect.at)
			: rewindCapturedMessage(message, row.origin, effect.fromSeq);
		if (message === null) return null;
	}
	return { ...row, message };
}

function hydrate(carry: Carry, records: ProjectionRow[]): Set<string> {
	if (records.length > PROJECTION_LIMITS.referencedRows) throw new ProjectionUnsupported('row_limit');
	if (records.reduce((size, row) => size + byteSize(row.body), 0) > PROJECTION_LIMITS.inputBytes) {
		throw new ProjectionUnsupported('input_limit');
	}
	const loaded = new Set<string>();
	const messages = new Map(carry.state.messages.map(message => [message.id, message]));
	for (const record of records) {
		if (loaded.has(record.id)) throw new ProjectionUnsupported('duplicate_row');
		loaded.add(record.id);
		const captured = decode(record.body) as CapturedRow;
		if (!captured?.message || captured.message.id !== record.id ||
			captured.order !== record.order || captured.origin !== record.origin ||
			captured.foldedThrough !== record.foldedThrough ||
			captured.foldedThrough > carry.state.lastSeq) throw new ProjectionUnsupported('stale_projection');
		if (messages.has(record.id)) continue; // explicit preview/current-carry override
		const row = applyEffects(captured, carry.effects);
		if (!row) continue;
		messages.set(record.id, row.message);
		carry.slots.set(record.id, { order: row.order, originOrder: row.originOrder, terminal: row.terminal });
		carry.state.messageOriginSeq.set(record.id, row.origin);
		carry.originOrders.set(record.id, row.originOrder);
	}
	carry.state.messages = [...messages.values()].sort((a, b) => carry.slots.get(a.id)!.order - carry.slots.get(b.id)!.order);
	carry.state.messageOriginSeq = new Map([...carry.state.messageOriginSeq].sort(
		([a], [b]) => carry.originOrders.get(a)! - carry.originOrders.get(b)!));
	return loaded;
}

function snapshot(carry: Carry): Carry {
	const state = sealState(carry.state);
	return { ...carry, state: { ...state, messageOriginSeq: new Map(state.messageOriginSeq) },
		slots: new Map(carry.slots), originOrders: new Map(carry.originOrders), effects: [...carry.effects] };
}

function indexFold(carry: Carry, before: Map<string, ChatMessage>, event: WireEvent): void {
	const present = new Set<string>();
	let previous = -1;
	for (const message of carry.state.messages) {
		if (present.has(message.id)) throw new ProjectionUnsupported('duplicate_message');
		present.add(message.id);
		let slot = carry.slots.get(message.id) ?? { order: carry.nextOrder++, originOrder: -1, terminal: false };
		if (slot.order <= previous) throw new ProjectionUnsupported('message_reordered');
		previous = slot.order;
		const terminalEnvelope = (message.type === 'tool_use' && event.type === 'tool.result') ||
			(message.type === 'subagent' && (event.type === 'subagent.completed' ||
				(event.type === 'task.updated' && carry.state.activeTaskSummaries.some(task =>
					task.id === event.task_id && ['completed', 'error', 'killed'].includes(task.status)))));
		const targets = [event.tool_use_id, event.task_id, event.agent_session_id];
		const targeted = messageAliases(message).some(alias => targets.includes(alias));
		const terminalStatus = message.type === 'tool_use'
			? message.toolStatus === 'complete' || message.toolStatus === 'error'
			: message.type === 'subagent' && ['completed', 'error', 'stopped'].includes(message.agentStatus ?? '');
		const terminal = terminalStatus && (slot.terminal ||
			(terminalEnvelope && targeted && before.get(message.id) !== message));
		slot = { ...slot, terminal };
		carry.slots.set(message.id, slot);
	}
	for (const id of carry.slots.keys()) if (!present.has(id)) carry.slots.delete(id);
	previous = -1;
	for (const [id] of carry.state.messageOriginSeq) {
		const order = carry.originOrders.get(id) ?? carry.nextOriginOrder++;
		if (order <= previous) throw new ProjectionUnsupported('origin_reordered');
		previous = order;
		carry.originOrders.set(id, order);
		const slot = carry.slots.get(id);
		if (slot) carry.slots.set(id, { ...slot, originOrder: order });
	}
	for (const id of carry.originOrders.keys()) if (!carry.state.messageOriginSeq.has(id)) carry.originOrders.delete(id);
}

function sealable(carry: Carry, message: ChatMessage, margin: Set<string>): boolean {
	const origin = carry.state.messageOriginSeq.get(message.id);
	if (origin === undefined || origin > (carry.state.attemptStartSeq ?? 0) || margin.has(message.id) ||
		message.streaming || message.id === carry.state.openTextId || message.id === carry.state.openThinkingId ||
		carry.state.speculativelySettledCardIds.includes(message.id)) return false;
	if (message.role === 'user') return !message.metadata?.not_sent;
	if (message.role !== 'assistant') return false; // approvals, retry and unknown cards stay mutable
	if (message.type === 'tool_use' || message.type === 'subagent') return carry.slots.get(message.id)?.terminal === true;
	return !message.type || message.type === 'text' || message.type === 'thinking';
}

function finish(input: Carry, loaded: Set<string>, seal: boolean): ProjectionDelta {
	const carry = snapshot(input);
	const upserts: ProjectionRow[] = [];
	const kept: ChatMessage[] = [];
	const margin = new Set(carry.state.messages.slice(-PROJECTION_LIMITS.marginRows).map(message => message.id));
	for (const message of carry.state.messages) {
		const origin = carry.state.messageOriginSeq.get(message.id);
		const slot = carry.slots.get(message.id)!;
		const captured: CapturedRow = { ...slot, message, origin: origin ?? -1, foldedThrough: carry.state.lastSeq };
		const body = seal && sealable(carry, message, margin) ? encode(captured) : null;
		if (body !== null && byteSize(body) <= PROJECTION_LIMITS.rowBytes) {
			upserts.push({ id: message.id, order: slot.order, origin: origin!,
				foldedThrough: captured.foldedThrough, aliases: messageAliases(message), body });
			carry.slots.delete(message.id);
			carry.originOrders.delete(message.id);
			carry.state.messageOriginSeq.delete(message.id);
		} else kept.push(message);
	}
	carry.state = { ...carry.state, messages: kept };
	const body = encode(carry);
	if (byteSize(body) > PROJECTION_LIMITS.carryBytes) throw new ProjectionUnsupported('carry_limit');
	const retained = new Set(upserts.map(row => row.id));
	return { carry: body, upserts, removed: seal ? [...loaded].filter(id => !retained.has(id)) : [],
		lastSeq: carry.state.lastSeq, effectCount: carry.effects.length };
}

/** Fold one bounded canonical-input quantum. The server rebuilds the bounded
 * raw attempt tail, rather than appending prematurely flushed delta runs.
 */
export function foldProjection(body: string | null, values: unknown[], records: ProjectionRow[] = []): ProjectionFold {
	const carry = readCarry(body);
	const loaded = hydrate(carry, records);
	let boundary: Carry | null = null;
	for (const event of frames(values)) {
		if (event.seq <= carry.state.lastSeq) throw new ProjectionUnsupported('non_monotonic_input');
		const before = new Map(carry.state.messages.map(message => [message.id, message]));
		const previousBoundary = carry.state.attemptStartSeq;
		const result = reduceWithMessageEffects(carry.state, event);
		carry.state = result.state;
		carry.effects = [...carry.effects, ...result.effects];
		if (carry.effects.length > PROJECTION_LIMITS.effects) throw new ProjectionUnsupported('effect_limit');
		indexFold(carry, before, event);
		if (carry.state.attemptStartSeq !== previousBoundary) boundary = snapshot(carry);
	}
	return { boundary: boundary ? finish(boundary, loaded, true) : null, preview: finish(carry, loaded, false) };
}

/** Test/read adapter: reconstitute actual data, never normalize expected state.
 * API reads remain disabled; callers must validate generation/source first.
 */
export function materializeProjection(body: string, records: ProjectionRow[], epoch: string): V2SessionState {
	const carry = readCarry(body);
	hydrate(carry, records);
	return withLogEpoch(carry.state, epoch);
}
