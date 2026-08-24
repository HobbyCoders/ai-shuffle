import type { ChatMessage } from '$lib/stores/types';

export interface UserTurnNavigationItem {
	id: string;
	label: string;
	title: string;
	preview: string;
	/** 0–1 bar length, derived from prompt size so the rail reads as a minimap. */
	weight: number;
}

/** Stable, DOM-safe transcript anchor derived from the message identity. */
export function turnAnchorId(messageId: string): string {
	return `user-turn-${encodeURIComponent(messageId)}`;
}

export function compactTurnLabel(content: string, maxLength = 72): string {
	const compact = content.replace(/\s+/g, ' ').trim() || 'User message';
	return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact;
}

/** First non-empty line becomes the hover title; remaining lines are the body. */
export function splitTurnPreview(content: string): { title: string; preview: string } {
	const lines = content
		.replace(/\r\n/g, '\n')
		.split('\n')
		.map((line) => line.trim())
		.filter(Boolean);
	if (lines.length === 0) return { title: 'User message', preview: '' };
	if (lines.length === 1) {
		const line = lines[0];
		if (line.length <= 64) return { title: line, preview: '' };
		return { title: compactTurnLabel(line, 64), preview: line };
	}
	return {
		title: compactTurnLabel(lines[0], 64),
		preview: lines.slice(1).join(' ')
	};
}

export function turnBarWeight(content: string): number {
	const len = content.replace(/\s+/g, ' ').trim().length;
	if (len <= 24) return 0.38;
	if (len >= 360) return 1;
	return 0.38 + ((len - 24) / 336) * 0.62;
}

export function isNavigableUserTurn(message: ChatMessage): boolean {
	return message.role === 'user' && message.type !== 'command';
}

export function deriveUserTurnNavigationItems(messages: ChatMessage[]): UserTurnNavigationItem[] {
	const seen = new Set<string>();
	const result: UserTurnNavigationItem[] = [];
	for (const message of messages) {
		if (!isNavigableUserTurn(message) || !message.id || seen.has(message.id)) continue;
		seen.add(message.id);
		result.push({
			id: message.id,
			label: compactTurnLabel(message.content),
			...splitTurnPreview(message.content),
			weight: turnBarWeight(message.content)
		});
	}
	return result;
}

/**
 * When the running turn actually began, for the live "Working for Ns" readout.
 *
 * The anchor is the user message that OPENED the turn, not the newest one in the
 * transcript. A prompt steered into a turn that is already running is recorded
 * where the model received it and carries the running turn's id, so treating the
 * last user message as the start restarted the clock mid-turn (45s of work
 * reported as 32s). Same rule the settled fold uses: only a different turn id
 * moves the anchor.
 *
 * `liveTurnId` is preferred when known; otherwise the trailing user message's own
 * turn id delimits the turn. Messages with no turn id (optimistic sends, legacy
 * transcripts) keep the plain one-turn-per-user-message behaviour.
 */
export function resolveLiveTurnStartedAt(
	messages: ChatMessage[],
	liveTurnId?: string | null
): string | null {
	let lastUserIndex = -1;
	for (let index = messages.length - 1; index >= 0; index--) {
		if (isNavigableUserTurn(messages[index])) { lastUserIndex = index; break; }
	}
	if (lastUserIndex === -1) return messages[0]?.timestamp ?? null;

	const turnId = liveTurnId || messages[lastUserIndex].turnId;
	// No id to group by, or the trailing prompt belongs to some other turn than
	// the live one: the newest prompt is the best anchor available.
	if (!turnId || (messages[lastUserIndex].turnId && messages[lastUserIndex].turnId !== turnId)) {
		return messages[lastUserIndex].timestamp ?? null;
	}

	let anchor = messages[lastUserIndex];
	for (let index = lastUserIndex - 1; index >= 0; index--) {
		const message = messages[index];
		if (!isNavigableUserTurn(message)) continue;
		if (message.turnId !== turnId) break;
		anchor = message;
	}
	return anchor.timestamp ?? null;
}

/** Last turn whose top has crossed the reading line; the first turn wins above it. */
export function resolveCurrentTurnIndex(turnTops: number[], readingLine: number): number {
	if (turnTops.length === 0) return 0;
	let current = 0;
	let seen = false;
	for (let index = 0; index < turnTops.length; index++) {
		const top = turnTops[index];
		// Missing anchors (virtualized / not yet laid out) must not freeze the
		// rail on turn 0: skip them and keep scanning later measurable ticks.
		if (!Number.isFinite(top)) continue;
		if (top <= readingLine) {
			current = index;
			seen = true;
		} else if (seen) {
			break;
		}
	}
	return current;
}

export function resolveTurnNavigationIndex(
	currentIndex: number,
	itemCount: number,
	delta: -1 | 1
): number {
	if (itemCount <= 0) return 0;
	return Math.max(0, Math.min(itemCount - 1, currentIndex + delta));
}
