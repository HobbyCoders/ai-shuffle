import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import {
	compactTurnLabel,
	deriveUserTurnNavigationItems,
	isNavigableUserTurn,
	resolveCurrentTurnIndex,
	resolveLiveTurnStartedAt,
	resolveTurnNavigationIndex,
	splitTurnPreview,
	turnAnchorId,
	turnBarWeight
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
		expect(items.map(({ id, label, title, preview }) => ({ id, label, title, preview }))).toEqual([
			{ id: 'user/one', label: 'First prompt', title: 'First', preview: 'prompt' },
			{ id: 'user-two', label: 'Second prompt', title: 'Second prompt', preview: '' }
		]);
		expect(items[0].weight).toBeGreaterThan(0);
		expect(items[0].weight).toBeLessThanOrEqual(1);
		expect(turnAnchorId(items[0].id)).toBe('user-turn-user%2Fone');
	});

	it('compacts labels and provides an empty-content fallback', () => {
		expect(compactTurnLabel('   ')).toBe('User message');
		expect(compactTurnLabel('abcdefgh', 6)).toBe('abcde…');
	});

	it('splits the first line as the hover title', () => {
		expect(splitTurnPreview('')).toEqual({ title: 'User message', preview: '' });
		expect(splitTurnPreview('Short ask')).toEqual({ title: 'Short ask', preview: '' });
		expect(splitTurnPreview('Also theres a bug\nImplemented the mobile composer updates')).toEqual({
			title: 'Also theres a bug',
			preview: 'Implemented the mobile composer updates'
		});
		const long = 'A'.repeat(80);
		expect(splitTurnPreview(long)).toEqual({
			title: `${'A'.repeat(63)}…`,
			preview: long
		});
	});

	it('maps longer prompts to longer timeline bars', () => {
		expect(turnBarWeight('hi')).toBe(0.38);
		expect(turnBarWeight('A'.repeat(400))).toBe(1);
		expect(turnBarWeight('A'.repeat(100))).toBeGreaterThan(turnBarWeight('A'.repeat(40)));
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

describe('live turn anchor', () => {
	// The live "Working for Ns" readout hangs off this. A prompt steered into a
	// running turn lands in the transcript mid-turn and carries the running
	// turn's id — anchoring on it restarted the clock (45s of work shown as 32s).

	const started = '2026-08-23T12:00:00.000Z';
	const steered = '2026-08-23T12:00:13.000Z';

	it('stays on the prompt that opened the turn when a message is steered into it', () => {
		const messages = [
			message({ id: 'u1', role: 'user', content: 'go', turnId: 'turn-1', timestamp: started }),
			message({ id: 'a1', content: 'working' }),
			message({ id: 'u2', role: 'user', content: 'also this', turnId: 'turn-1', timestamp: steered })
		];
		expect(resolveLiveTurnStartedAt(messages, 'turn-1')).toBe(started);
		// Same answer without the live turn id — the trailing prompt's own id groups it.
		expect(resolveLiveTurnStartedAt(messages)).toBe(started);
	});

	it('moves to a genuinely new turn', () => {
		const nextTurn = '2026-08-23T12:01:00.000Z';
		const messages = [
			message({ id: 'u1', role: 'user', content: 'go', turnId: 'turn-1', timestamp: started }),
			message({ id: 'u2', role: 'user', content: 'also this', turnId: 'turn-1', timestamp: steered }),
			message({ id: 'u3', role: 'user', content: 'next', turnId: 'turn-2', timestamp: nextTurn })
		];
		expect(resolveLiveTurnStartedAt(messages, 'turn-2')).toBe(nextTurn);
	});

	it('anchors on the newest prompt when it belongs to no turn yet', () => {
		// Optimistic send, not yet reconciled onto its wire event.
		const messages = [
			message({ id: 'u1', role: 'user', content: 'go', turnId: 'turn-1', timestamp: started }),
			message({ id: 'u2', role: 'user', content: 'optimistic', timestamp: steered })
		];
		expect(resolveLiveTurnStartedAt(messages)).toBe(steered);
	});

	it('anchors on the newest prompt when the live turn is not the one it belongs to', () => {
		const messages = [
			message({ id: 'u1', role: 'user', content: 'go', turnId: 'turn-1', timestamp: started })
		];
		expect(resolveLiveTurnStartedAt(messages, 'turn-2')).toBe(started);
	});

	it('falls back to the first message when there is no user prompt', () => {
		expect(resolveLiveTurnStartedAt([message({ id: 'a1', timestamp: started })], 'turn-1')).toBe(started);
		expect(resolveLiveTurnStartedAt([], 'turn-1')).toBeNull();
	});
});
