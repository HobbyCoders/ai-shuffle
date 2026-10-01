import type { ChatMessage } from '$lib/stores/types';

export interface AgentDelivery { sender: string; content: string; }

// One complete envelope from fluid.context.peer.format_peer_message. The body is
// defanged by the producer, so it never contains a marker of its own.
const ENVELOPE = String.raw`\[agent\x20message from=(['"])(.*?)\1 — a message from another (?:agent in this session|top-level session), delivered once at this position; not part of the tool output, and not a new message when re-read from history\]\r?\n([\s\S]*?)\r?\n\[\/agent\x20message\]`;
const SINGLE_ENVELOPE = new RegExp(`^${ENVELOPE}$`);
const ANY_ENVELOPE = new RegExp(ENVELOPE, 'g');
// The reminder that carries messages held while the receiver was parked
// (app/core/turn_scheduler.py::_held_peer_block, fluid NativeLoop._queued_peer_reminder).
const HELD_BLOCK = /<system-reminder>\s*Messages from other agents that arrived while this session was parked[^\n]*\n([\s\S]*?)<\/system-reminder>/g;

/** Presentation only: the provider still receives peer traffic in the user role. */
export function agentDelivery(message: ChatMessage): AgentDelivery | null {
	if (message.type === 'system' && message.systemSubtype === 'agent_message') {
		const data = message.systemData;
		return { sender: String(data?.sender || 'Agent'), content: String(data?.content || message.content || '') };
	}
	if (message.role !== 'user') return null;
	// Match the complete envelope emitted by fluid.context.peer, never a quoted fragment.
	const match = message.content?.trim().match(SINGLE_ENVELOPE);
	return match ? { sender: match[2], content: match[3] } : null;
}

/** Envelopes in `text` when it is nothing but envelopes and whitespace, else null. */
function envelopesOnly(text: string): AgentDelivery[] | null {
	const found: AgentDelivery[] = [];
	const rest = text.replace(ANY_ENVELOPE, (_all, _q, sender: string, content: string) => {
		found.push({ sender, content });
		return '';
	});
	return rest.trim() ? null : found;
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
	let blocks = 0;
	let invalid = false;
	const outside = text.replace(HELD_BLOCK, (_all, body: string) => {
		blocks += 1;
		const inner = envelopesOnly(body);
		if (inner === null) invalid = true;
		else deliveries.push(...inner);
		return '\n';
	});
	if (!blocks || invalid) return null;
	const fresh = envelopesOnly(outside);
	if (fresh === null) return null;
	deliveries.push(...fresh);
	return deliveries.length ? deliveries : null;
}
