import type { ChatMessage, PendingPeerMessage } from '$lib/stores/types';
import { agentDelivery } from './agent-delivery';

export interface AgentMessageProjectionInput {
	sessionId: string | null;
	/** False for an idle or parked recipient; omitted keeps mid-turn grouping. */
	sessionActive?: boolean;
	messages: ChatMessage[];
	pendingPeerMessages: readonly PendingPeerMessage[];
}

/**
 * Presentation-only peer rows, projected before the transcript model is built.
 * Queue snapshots can clear before the receipt arrives, so retain the last
 * known queued row until delivery or a session change; clearing is not a receipt.
 * Delivered rows use delivery="delivered", including historical held receipts.
 * Queued senders carry only their session id, like text receipts; both resolve
 * names through agentSenderNames, never a queue-only label lost on reload.
 * Legacy deliveries without a turn id retain their existing message id.
 */
export function createAgentMessageProjection() {
	let sessionId: string | null | undefined;
	const pending = new Map<string, { peer: PendingPeerMessage; row: ChatMessage }>();
	const delivered = new Set<string>();
	let normalized = new WeakMap<ChatMessage, ChatMessage>();
	let previous: ChatMessage[] | undefined;
	let previousInput: AgentMessageProjectionInput | undefined;

	return {
		update(input: AgentMessageProjectionInput): ChatMessage[] {
			if (input.sessionId !== sessionId) {
				sessionId = input.sessionId;
				pending.clear();
				delivered.clear();
				normalized = new WeakMap();
				previous = undefined;
				previousInput = undefined;
			}
			if (previous && previousInput?.messages === input.messages &&
				previousInput.pendingPeerMessages === input.pendingPeerMessages &&
				previousInput.sessionActive === input.sessionActive) return previous;

			const rows = input.messages.map((message) => {
				if (!agentDelivery(message)) return message;
				const key = message.queuedTurnId ?? message.turnId ?? message.id;
				delivered.add(key);
				pending.delete(key);
				let row = normalized.get(message);
				if (!row) {
					row = message.id === key && message.serverId === key && message.systemData?.delivery === 'delivered'
						? message
						: { ...message, id: key, serverId: key, systemData: { ...message.systemData, delivery: 'delivered' } };
					normalized.set(message, row);
				}
				return row;
			});

			for (const peer of input.pendingPeerMessages) {
				if (delivered.has(peer.id)) continue;
				const existing = pending.get(peer.id);
				const known = existing?.peer;
				if (known && known.content === peer.content && known.senderSessionId === peer.senderSessionId &&
					Object.is(known.queuedAt, peer.queuedAt)) continue;
				// Predict the wake at first sighting, not when turn.started races its receipt.
				const agentWake = existing ? existing.row.metadata?.agent_wake === true : input.sessionActive === false;
				const timestamp = new Date(peer.queuedAt);
				pending.set(peer.id, {
					peer: { ...peer },
					row: {
						id: peer.id,
						serverId: peer.id,
						turnId: peer.id,
						role: 'system',
						type: 'system',
						systemSubtype: 'agent_message',
						content: peer.content,
						...(agentWake ? { metadata: { agent_wake: true } } : {}),
						...(Number.isFinite(timestamp.getTime()) ? { timestamp: timestamp.toISOString() } : {}),
						systemData: {
							sender: peer.senderSessionId || 'Agent',
							content: peer.content,
							delivery: 'queued'
						}
					}
				});
			}
			for (const { row } of pending.values()) rows.push(row);

			previousInput = { ...input };
			if (previous && rows.length === previous.length && rows.every((row, index) => row === previous![index])) return previous;
			previous = rows.length === input.messages.length && rows.every((row, index) => row === input.messages[index])
				? input.messages : rows;
			return previous;
		}
	};
}
