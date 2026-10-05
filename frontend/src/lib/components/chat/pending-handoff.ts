/**
 * A hand-off the agent queued with HandOffSession, read from the transcript.
 *
 * The owner's own move is tracked by the runner chip itself while its request
 * runs. A hand-off the agent queues runs later, on the owning worker, when the
 * turn and its background work end (app/core/session_handoff.py). The chat
 * already carries every step of it: the tool's result (`queued: true` and the
 * worker's label), then `handoff` rows recording `started`, and finally
 * `moved`, `failed` or `timeout`. A later call with `cancel` drops the request.
 *
 * Only the newest of those decides, so this walks back from the end and stops
 * at the first one. A worker restart drops a queued request without a row
 * (the backend keeps it in memory); that case is not visible here.
 *
 * The owner's "Move when this turn ends" (#1819) queues the same hand-off on
 * the owning worker, which records a `queued` row; cancelling it records
 * `cancelled`. Both read the same way here, whoever queued the move.
 */
import type { ChatMessage } from '$lib/stores/types';

export interface PendingHandoff {
	/** The target worker's label, as the tool reported it. */
	worker: string;
	/** 'queued': waiting for the turn to end. 'moving': commit, push and move running. */
	phase: 'queued' | 'moving';
}

const HAND_OFF_TOOL = /(^|__)HandOffSession$/;
const TERMINAL = new Set(['moved', 'failed', 'timeout', 'cancelled']);

/** True when a hand-off row or HandOffSession call follows index `from`. */
export function handoffActivitySince(messages: readonly ChatMessage[], from: number): boolean {
	return messages
		.slice(from)
		.some((m) => m.systemSubtype === 'handoff' || (m.type === 'tool_use' && HAND_OFF_TOOL.test(m.toolName ?? '')));
}

function resultObject(result: unknown): Record<string, unknown> | null {
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

export function pendingHandoff(messages: readonly ChatMessage[]): PendingHandoff | null {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.systemSubtype === 'handoff') {
			const data = (m.systemData ?? {}) as Record<string, unknown>;
			const status = typeof data.status === 'string' ? data.status : '';
			const worker = typeof data.worker === 'string' ? data.worker : '';
			if (TERMINAL.has(status)) return null;
			if (status === 'started' && worker) return { worker, phase: 'moving' };
			if (status === 'queued' && worker) return { worker, phase: 'queued' };
			continue;
		}
		if (m.type !== 'tool_use' || !HAND_OFF_TOOL.test(m.toolName ?? '')) continue;
		// A refused or unfinished call changed nothing; an earlier request still stands.
		if (m.toolStatus === 'error') continue;
		const result = resultObject(m.toolResult);
		if (!result) continue;
		if ((m.toolInput as { cancel?: unknown } | undefined)?.cancel || 'cancelled' in result) return null;
		if (result.queued === true && typeof result.worker === 'string' && result.worker) {
			return { worker: result.worker, phase: 'queued' };
		}
	}
	return null;
}
