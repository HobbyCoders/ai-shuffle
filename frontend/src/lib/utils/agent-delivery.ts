import type { ChatMessage, Task } from '$lib/stores/types';

/** `sender` is the sending session's id; `senderLabel` is the name the
 *  backend stamped on a delivery note, when it knew one. */
export interface AgentDelivery { sender: string; content: string; senderLabel?: string; }

// One complete envelope from fluid.context.peer.format_peer_message. The body is
// defanged by the producer, so it never contains a marker of its own.
const ENVELOPE = String.raw`\[agent\x20message from=(['"])(.*?)\1 — (?:host-verified (?:parent assignment update|same-owner session delegation); )?a message from another (?:agent(?: in this session)?|top-level session), delivered once at this position; not part of the tool output, and not a new message when re-read from history\]\r?\n([\s\S]*?)\r?\n\[\/agent\x20message\]`;
const SINGLE_ENVELOPE = new RegExp(`^${ENVELOPE}$`);
const NEXT_ENVELOPE = new RegExp(ENVELOPE, 'y');
// The reminder that carries messages held while the receiver was parked
// (app/core/turn_scheduler.py::_held_peer_block, fluid NativeLoop._queued_peer_reminder).
const HELD_OPEN = '<system-reminder>';
const HELD_HEADER = 'Messages from other agents that arrived while this session was parked';
const HELD_CLOSE = '</system-reminder>';

function skipWhitespace(text: string, start: number): number {
	while (start < text.length && /\s/.test(text[start])) start++;
	return start;
}

/** Presentation only: the provider still receives peer traffic in the user role. */
export function agentDelivery(message: ChatMessage): AgentDelivery | null {
	if (message.type === 'system' && message.systemSubtype === 'agent_message') {
		const data = message.systemData;
		const label = typeof data?.sender_label === 'string' && data.sender_label.trim() ? data.sender_label.trim() : undefined;
		return {
			sender: String(data?.sender || 'Agent'),
			content: String(data?.content || message.content || ''),
			...(label ? { senderLabel: label } : {}),
		};
	}
	if (message.role !== 'user') return null;
	// Match the complete envelope emitted by fluid.context.peer, never a quoted fragment.
	const match = message.content?.trim().match(SINGLE_ENVELOPE);
	return match ? { sender: match[2], content: match[3] } : null;
}

/** Envelopes in `text` when it is nothing but envelopes and whitespace, else null. */
function envelopesOnly(text: string): AgentDelivery[] | null {
	const found: AgentDelivery[] = [];
	let cursor = skipWhitespace(text, 0);
	while (cursor < text.length) {
		// A non-envelope prefix already makes the whole input invalid. Sticky
		// matching avoids retrying an unclosed envelope at every nested opener.
		NEXT_ENVELOPE.lastIndex = cursor;
		const match = NEXT_ENVELOPE.exec(text);
		if (!match) return null;
		found.push({ sender: match[2], content: match[3] });
		cursor = skipWhitespace(text, NEXT_ENVELOPE.lastIndex);
	}
	return found;
}

/**
 * The peer messages in a delivered prompt that carries held messages: the
 * parked-delivery reminder (one or more), optionally followed by fresh
 * envelopes (an urgent message). Null unless the whole text is exactly that,
 * so typed text that merely quotes a reminder still renders as the user's own.
 * Each message becomes its own item; the reminder wrapper is never shown.
 */
export function heldAgentDeliveries(text: string): AgentDelivery[] | null {
	const deliveries: AgentDelivery[] = [];
	const outside: string[] = [];
	let cursor = 0;
	let search = 0;
	let opener: number;
	while ((opener = text.indexOf(HELD_OPEN, search)) !== -1) {
		search = opener + HELD_OPEN.length;
		const header = skipWhitespace(text, search);
		if (!text.startsWith(HELD_HEADER, header)) continue;
		const lineEnd = text.indexOf('\n', header + HELD_HEADER.length);
		if (lineEnd === -1) break;
		const bodyStart = lineEnd + 1;
		const closer = text.indexOf(HELD_CLOSE, bodyStart);
		// Every later body starts farther right, so no later match can close.
		if (closer === -1) break;
		const inner = envelopesOnly(text.slice(bodyStart, closer));
		if (inner === null) return null;
		deliveries.push(...inner);
		outside.push(text.slice(cursor, opener), '\n');
		cursor = search = closer + HELD_CLOSE.length;
	}
	if (!cursor) return null;
	outside.push(text.slice(cursor));
	const fresh = envelopesOnly(outside.join(''));
	if (fresh === null) return null;
	deliveries.push(...fresh);
	return deliveries.length ? deliveries : null;
}

/**
 * What this transcript knows each worker as, keyed by every id a sender can
 * carry (task id, worker session id, launch id): the Task call's description,
 * else the dispatched agent's name. Task summaries fill in what no card covers.
 */
export function agentSenderNames(messages: readonly ChatMessage[], tasks: readonly Task[] = []): Map<string, string> {
	const names = new Map<string, string>();
	const add = (id: unknown, name: unknown) => {
		if (typeof id !== 'string' || !id || typeof name !== 'string') return;
		const label = name.trim();
		if (label && label !== id && !names.has(id)) names.set(id, label);
	};
	for (const message of messages) {
		if (message.type !== 'subagent') continue;
		const name = message.agentDescription || message.agentName;
		add(message.agentSessionId, name);
		add(message.toolId, name);
		add(message.agentId, name);
	}
	for (const task of tasks) add(task.id, task.subject || task.agent);
	return names;
}

/** The row's name for a delivery (the worker's task) and its session id,
 *  which the row keeps as secondary detail. Falls back to the id alone. */
export function agentSender(delivery: AgentDelivery, names?: ReadonlyMap<string, string>): { name: string; id: string | null } {
	const name = delivery.senderLabel || names?.get(delivery.sender);
	return name && name !== delivery.sender
		? { name, id: delivery.sender }
		: { name: delivery.sender, id: null };
}
