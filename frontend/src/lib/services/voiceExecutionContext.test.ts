import { describe, expect, it, vi } from 'vitest';
const update = vi.hoisted(() => vi.fn(async (_owner: string, _context: unknown) => ({})));
vi.mock('$lib/api/voice', () => ({ updateVoiceContext: update }));
import { persistVoiceContext, voiceContextFor } from './voiceExecutionContext';
import type { ActiveSessionState } from '$lib/stores/active-session';
import { assistantState } from '$lib/stores/assistant';
import type { AssistantState } from '$lib/api/assistant';

describe('voice execution context', () => {
	it('does not replace inherited agent permissions with default', () => {
		const state = { permissionModeOverride: null } as ActiveSessionState;
		expect(voiceContextFor(state).permission_mode).toBeNull();
		expect(JSON.stringify(voiceContextFor(state))).toContain('"permission_mode":null');
		state.permissionModeOverride = 'bypassPermissions';
		expect(voiceContextFor(state).permission_mode).toBe('bypassPermissions');
		state.permissionModeOverride = 'default';
		expect(voiceContextFor(state).permission_mode).toBe('default');
	});
	it('explicitly clears inherited effort and inapplicable model context', () => {
		const context = voiceContextFor({ modelOverride: 'unknown-model', effortLevelOverride: null, contextWindowChoice: '1m' } as unknown as ActiveSessionState);
		expect(context.effort).toBeNull();
		expect(context.context_window).toBeNull();
	});
	it('carries no effort from the assistant home', () => {
		assistantState.set({ home_session_id: 'home' } as AssistantState);
		try {
			const state = { sessionId: 'home', modelOverride: 'opus', effortLevelOverride: 'low' } as unknown as ActiveSessionState;
			expect(voiceContextFor(state)).not.toHaveProperty('effort');
			expect(voiceContextFor({ ...state, sessionId: 'ordinary' }).effort).toBe('low');
		} finally {
			assistantState.set(null);
		}
	});
	it('deduplicates identical settings', async () => {
		const before = update.mock.calls.length;
		const first = persistVoiceContext('dedupe', { permission_mode: 'default' });
		expect(persistVoiceContext('dedupe', { permission_mode: 'default' })).toBe(first);
		await first;
		expect(update.mock.calls.length - before).toBe(1);
	});
	it('orders writes for one owner without blocking a different session', async () => {
		let release!: () => void;
		update.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({}); }));
		const first = persistVoiceContext('ordered', { effort: 'low' });
		await Promise.resolve();
		const second = persistVoiceContext('ordered', { effort: 'high' });
		await persistVoiceContext('other', { effort: 'medium' });
		expect(update).not.toHaveBeenCalledWith('ordered', { effort: 'high' });
		release();
		await Promise.all([first, second]);
		expect(update).toHaveBeenLastCalledWith('ordered', { effort: 'high' });
	});
	it('retries a failed setting instead of treating it as saved', async () => {
		update.mockRejectedValueOnce(new Error('offline'));
		await expect(persistVoiceContext('retry', { fast: false })).rejects.toThrow('offline');
		await expect(persistVoiceContext('retry', { fast: false })).resolves.toBeUndefined();
	});
});
