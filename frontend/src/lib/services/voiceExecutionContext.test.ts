import { describe, expect, it, vi } from 'vitest';
const update = vi.hoisted(() => vi.fn(async (_owner: string, _context: unknown) => ({})));
vi.mock('$lib/api/voice', () => ({ updateVoiceContext: update }));
import { persistVoiceContext, voiceContextFor } from './voiceExecutionContext';
import type { ActiveSessionState } from '$lib/stores/active-session';

describe('voice execution context', () => {
	it('does not replace inherited agent permissions with default', () => {
		const state = { permissionModeOverride: null } as ActiveSessionState;
		expect(voiceContextFor(state).permission_mode).toBeUndefined();
		expect(JSON.stringify(voiceContextFor(state))).not.toContain('permission_mode');
		state.permissionModeOverride = 'bypassPermissions';
		expect(voiceContextFor(state).permission_mode).toBe('bypassPermissions');
		state.permissionModeOverride = 'default';
		expect(voiceContextFor(state).permission_mode).toBe('default');
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
