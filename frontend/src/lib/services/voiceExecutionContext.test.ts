import { describe, expect, it, vi } from 'vitest';
const update = vi.hoisted(() => vi.fn(async (_owner: string, _context: unknown) => ({})));
vi.mock('$lib/api/voice', () => ({ updateVoiceContext: update }));
import { persistVoiceContext, voiceContextFor } from './voiceExecutionContext';
import type { ActiveSessionState } from '$lib/stores/active-session';

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
	it('carries the picked effort in every chat', () => {
		const state = { sessionId: 'chat', modelOverride: 'opus', effortLevelOverride: 'low' } as unknown as ActiveSessionState;
		expect(voiceContextFor(state).effort).toBe('low');
	});
	it('leaves out an open chat\'s access and Fast while they are still loading (#418)', () => {
		const state = {
			sessionId: 'worker', modelOverride: 'opus', permissionModeOverride: null, fastMode: false,
			bindingUnhydrated: { permission: true, fast: true },
		} as unknown as ActiveSessionState;
		const loading = voiceContextFor(state);
		expect(loading).not.toHaveProperty('permission_mode');
		expect(loading).not.toHaveProperty('fast');
		expect(loading.model).toBe('opus');

		const known = voiceContextFor({ ...state, permissionModeOverride: 'bypassPermissions', fastMode: true, bindingUnhydrated: {} });
		expect(known).toMatchObject({ permission_mode: 'bypassPermissions', fast: true });
		// A new compose has no session binding to keep: it always says what it shows.
		expect(voiceContextFor({ ...state, sessionId: null })).toMatchObject({ permission_mode: null, fast: false });
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
