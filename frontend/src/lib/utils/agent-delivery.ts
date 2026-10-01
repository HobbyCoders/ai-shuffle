import type { ChatMessage } from '$lib/stores/types';

export interface AgentDelivery { sender: string; content: string; }

/** Presentation only: the provider still receives peer traffic in the user role. */
export function agentDelivery(message: ChatMessage): AgentDelivery | null {
	if (message.type === 'system' && message.systemSubtype === 'agent_message') {
		const data = message.systemData;
		return { sender: String(data?.sender || 'Agent'), content: String(data?.content || message.content || '') };
	}
	if (message.role !== 'user') return null;
	// Match the complete envelope emitted by fluid.context.peer, never a quoted fragment.
	const match = message.content?.trim().match(/^\[agent\x20message from=(['"])(.*?)\1 — a message from another (?:agent in this session|top-level session), delivered once at this position; not part of the tool output, and not a new message when re-read from history\]\r?\n([\s\S]*)\r?\n\[\/agent\x20message\]$/);
	return match ? { sender: match[2], content: match[3] } : null;
}
