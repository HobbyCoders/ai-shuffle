import type { ChatMessage, PendingPeerMessage } from '$lib/stores/types';
import { agentDelivery } from './agent-delivery';

export interface AgentMessageProjectionInput {
	sessionId: string | null;
	/** False for an idle or parked recipient; omitted keeps mid-turn grouping. */
	sessionActive?: boolean;
	/** An interrupt marks cancellation candidates, never proof of cancellation. */
	queueAbortSeq?: number;
	/** False only when fully settled: no live/parked work, loading or starting turn. */
	sessionUnsettled?: boolean;
	messages: ChatMessage[];
	pendingPeerMessages: readonly PendingPeerMessage[];
}

/**
 * Presentation-only peer rows, projected before the transcript model is built.
 * Queue snapshots can clear before the receipt arrives, so retain the last
 * known queued row until delivery or cancellation. A missing queued row is
 * cancelled only when the recipient is explicitly fully settled; an interrupt
 * alone is ambiguous because Send now also interrupts without cancelling peers.
 * Active, parked and starting turns retain the queue-clear-before-receipt gap.
 * Delivered rows use delivery="delivered", including historical held receipts.
 * Queued senders carry only their session id, like text receipts; both resolve
 * names through agentSenderNames, never a queue-only label lost on reload.
 * Legacy deliveries without a turn id retain their existing message id.
 * If a peer outlives its wake cohort and is drained as a separate later turn,
 * its authoritative receipt may still regroup it; that future wake is not known
 * when the peer is queued.
 */
export function createAgentMessageProjection() {
	let sessionId: string | null | undefined;
	const pending = new Map<string, { peer: PendingPeerMessage; row: ChatMessage }>();
	const retired = new Set<string>();
	const cancellationCandidates = new Set<string>();
	let lastAbortSeq = 0;
	let idleWakeKey: string | undefined;
	let normalized = new WeakMap<ChatMessage, ChatMessage>();
	let previous: ChatMessage[] | undefined;
	let previousInput: AgentMessageProjectionInput | undefined;

	return {
		update(input: AgentMessageProjectionInput): ChatMessage[] {
			if (input.sessionId !== sessionId) {
				sessionId = input.sessionId;
				pending.clear();
				retired.clear();
				cancellationCandidates.clear();
				lastAbortSeq = 0;
				idleWakeKey = undefined;
				normalized = new WeakMap();
				previous = undefined;
				previousInput = undefined;
			}
			if (previous && previousInput?.messages === input.messages &&
				previousInput.pendingPeerMessages === input.pendingPeerMessages &&
				previousInput.sessionActive === input.sessionActive &&
				previousInput.queueAbortSeq === input.queueAbortSeq &&
				previousInput.sessionUnsettled === input.sessionUnsettled) return previous;

			if ((input.queueAbortSeq ?? 0) > lastAbortSeq) {
				lastAbortSeq = input.queueAbortSeq!;
				for (const key of pending.keys()) cancellationCandidates.add(key);
			}
			// A successor running after Send now invalidates the interrupt's candidates.
			if (input.sessionActive === true) cancellationCandidates.clear();
			const rows = input.messages.map((message) => {
				if (!agentDelivery(message)) return message;
				const key = message.queuedTurnId ?? message.turnId ?? message.id;
				retired.add(key);
				pending.delete(key);
				cancellationCandidates.delete(key);
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
				if (retired.has(peer.id)) continue;
				const existing = pending.get(peer.id);
				const known = existing?.peer;
				if (known && known.content === peer.content && known.senderSessionId === peer.senderSessionId &&
					Object.is(known.queuedAt, peer.queuedAt)) continue;
				// Predict the wake at first sighting, not when turn.started races its receipt.
				const agentWake = existing ? existing.row.metadata?.agent_wake === true : input.sessionActive === false && !idleWakeKey;
				// Only the first peer wakes the recipient; its siblings steer into that turn.
				if (agentWake) idleWakeKey = peer.id;
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
			const queuedIds = new Set(input.pendingPeerMessages.map(peer => peer.id));
			const settled = input.sessionUnsettled === false && input.sessionActive !== true;
			// Queue-only Stop has no abort event, so settlement is sufficient itself.
			if (settled) {
				for (const key of pending.keys()) if (!queuedIds.has(key)) cancellationCandidates.add(key);
			}
			for (const key of cancellationCandidates) {
				// Stop and Send now share an interrupt: candidates still require settlement.
				if (!settled || queuedIds.has(key)) continue;
				pending.delete(key);
				retired.add(key); // Cancellation is sticky too: a stale snapshot cannot revive it.
				cancellationCandidates.delete(key);
			}
			for (const { row } of pending.values()) rows.push(row);
			if (pending.size === 0 && (input.sessionActive === true || (idleWakeKey && retired.has(idleWakeKey)))) {
				idleWakeKey = undefined;
			}

			previousInput = { ...input };
			if (previous && rows.length === previous.length && rows.every((row, index) => row === previous![index])) return previous;
			previous = rows.length === input.messages.length && rows.every((row, index) => row === input.messages[index])
				? input.messages : rows;
			return previous;
		}
	};
}
