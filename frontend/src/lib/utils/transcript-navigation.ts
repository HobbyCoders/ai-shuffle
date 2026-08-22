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
