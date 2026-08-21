import type { ChatMessage } from '$lib/stores/types';

export interface UserTurnNavigationItem {
	id: string;
	label: string;
}

/** Stable, DOM-safe transcript anchor derived from the message identity. */
export function turnAnchorId(messageId: string): string {
	return `user-turn-${encodeURIComponent(messageId)}`;
}

export function compactTurnLabel(content: string, maxLength = 72): string {
	const compact = content.replace(/\s+/g, ' ').trim() || 'User message';
	return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact;
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
		result.push({ id: message.id, label: compactTurnLabel(message.content) });
	}
	return result;
}

/** Last turn whose top has crossed the reading line; the first turn wins above it. */
export function resolveCurrentTurnIndex(turnTops: number[], readingLine: number): number {
	if (turnTops.length === 0) return 0;
	let current = 0;
	for (let index = 0; index < turnTops.length; index++) {
		if (turnTops[index] <= readingLine) current = index;
		else break;
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
