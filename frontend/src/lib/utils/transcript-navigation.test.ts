import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import {
	compactTurnLabel,
	deriveUserTurnNavigationItems,
	isNavigableUserTurn,
	resolveCurrentTurnIndex,
	resolveTurnNavigationIndex,
	turnAnchorId
} from './transcript-navigation';

function message(over: Partial<ChatMessage>): ChatMessage {
	return { id: 'message', role: 'assistant', content: '', ...over } as ChatMessage;
}

describe('transcript navigation derivation', () => {
	it('derives one item per unique user prompt and skips commands', () => {
		const first = message({ id: 'user/one', role: 'user', content: '  First\n\n prompt  ' });
		const items = deriveUserTurnNavigationItems([
			first,
			message({ id: 'answer', content: 'answer' }),
			first,
			message({ id: 'command', role: 'user', type: 'command', content: '/commit' }),
			message({ id: 'user-two', role: 'user', content: 'Second prompt' })
		]);
		expect(items).toEqual([
			{ id: 'user/one', label: 'First prompt' },
			{ id: 'user-two', label: 'Second prompt' }
		]);
		expect(turnAnchorId(items[0].id)).toBe('user-turn-user%2Fone');
	});

	it('compacts labels and provides an empty-content fallback', () => {
		expect(compactTurnLabel('   ')).toBe('User message');
		expect(compactTurnLabel('abcdefgh', 6)).toBe('abcde…');
	});

	it('recognizes only non-command user messages', () => {
		expect(isNavigableUserTurn(message({ role: 'user' }))).toBe(true);
		expect(isNavigableUserTurn(message({ role: 'user', type: 'command' }))).toBe(false);
		expect(isNavigableUserTurn(message({ role: 'assistant' }))).toBe(false);
	});
});

describe('transcript navigation position', () => {
	it('selects the last turn above the reading line', () => {
		expect(resolveCurrentTurnIndex([], 100)).toBe(0);
		expect(resolveCurrentTurnIndex([20, 180, 400], 100)).toBe(0);
		expect(resolveCurrentTurnIndex([-200, 20, 400], 100)).toBe(1);
		expect(resolveCurrentTurnIndex([-400, -200, -10], 100)).toBe(2);
	});

	it('clamps previous and next navigation to available turns', () => {
		expect(resolveTurnNavigationIndex(0, 3, -1)).toBe(0);
		expect(resolveTurnNavigationIndex(0, 3, 1)).toBe(1);
		expect(resolveTurnNavigationIndex(2, 3, 1)).toBe(2);
		expect(resolveTurnNavigationIndex(0, 0, 1)).toBe(0);
	});
});
