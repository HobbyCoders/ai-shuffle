/**
 * The slim chat-open replay builds the same store as the old fat one.
 *
 * The server now sends one compact context-usage snapshot per turn.usage
 * (no nested copy, no gridRows/mcpTools/skills) and merges consecutive
 * subagent text deltas into one subagent.progress frame. Both replays of the
 * same log come from `tests/api/test_events_sse_replay_budget.py`, which fails
 * when this fixture drifts from what the server sends.
 */

import { describe, it, expect } from 'vitest';
import { reduceAll, type V2SessionState, type WireEvent } from './reducer';
import { mapSdkContextUsage } from '$lib/components/chat/context-usage';
import type { SdkContextUsage } from '$lib/api/sessions';
import fixture from './__fixtures__/replay-usage-bloat.json';

const fat = fixture.fat as unknown as WireEvent[];
const slim = fixture.slim as unknown as WireEvent[];

function comparable(state: V2SessionState) {
	const { context_usage: _nested, ...usage } = (state.usage ?? {}) as Record<string, unknown>;
	return {
		...state,
		usage,
		// The bar renders the snapshot through this mapper; nothing else reads it.
		contextUsage: state.contextUsage
			? mapSdkContextUsage(state.contextUsage as unknown as SdkContextUsage)
			: null,
	};
}

describe('slim replay equivalence', () => {
	it('is smaller than the fat replay', () => {
		expect(slim.length).toBeLessThan(fat.length);
		expect(JSON.stringify(slim).length).toBeLessThan(JSON.stringify(fat).length);
	});

	it('folds to the same store state', () => {
		const a = reduceAll(fat);
		const b = reduceAll(slim);
		expect(comparable(b)).toEqual(comparable(a));
		expect(b.lastSeq).toBe(a.lastSeq);
	});

	it('keeps the subagent child rows and the context bar values', () => {
		const state = reduceAll(slim);
		const cards = state.messages.filter((m) => m.type === 'subagent');
		expect(cards.length).toBe(6);
		const children = cards.flatMap((c) => c.agentChildren ?? []);
		expect(children.some((c) => c.type === 'text' && c.content.includes('a0 a1 a2'))).toBe(true);
		expect(children.some((c) => c.type === 'tool_use' && c.toolResult === 'file body')).toBe(true);
		const bar = mapSdkContextUsage(state.contextUsage as unknown as SdkContextUsage);
		expect(bar.totalUsed).toBe(12_002);
		expect(bar.contextMax).toBe(200_000);
		expect(bar.rows.length).toBeGreaterThan(0);
	});

	it('gives the same state at every frame boundary of the slim replay', () => {
		// A dropped connection resumes at the last frame's seq, so each slim
		// prefix must equal the fat prefix up to that seq.
		let i = 0;
		for (let n = 1; n <= slim.length; n++) {
			const cursor = slim[n - 1].seq as number;
			while (i < fat.length && (fat[i].seq as number) <= cursor) i++;
			expect(comparable(reduceAll(slim.slice(0, n)))).toEqual(
				comparable(reduceAll(fat.slice(0, i)))
			);
		}
	});
});
