import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PreferenceSync } from '$lib/services/preference-sync';
import { createTaskChecks, MAX_TASK_MESSAGES, taskMessageKey } from './task-checks';

beforeEach(() => localStorage.clear());

describe('task-list ticks (#1813)', () => {
	it('keys a message by session and text, the same on every device', () => {
		expect(taskMessageKey('s1', '- [ ] a')).toBe(taskMessageKey('s1', '- [ ] a'));
		expect(taskMessageKey('s1', '- [ ] a')).not.toBe(taskMessageKey('s2', '- [ ] a'));
		expect(taskMessageKey('s1', '- [ ] a')).not.toBe(taskMessageKey('s1', '- [ ] b'));
	});

	it('a tick on one client reaches a second client of the same account', async () => {
		const server: Record<string, unknown> = {};
		const transport = {
			all: async () => structuredClone(server),
			get: async (key: string) => key in server ? { value: structuredClone(server[key]) } : null,
			put: vi.fn(async (key: string, value: unknown, onlyIfMissing?: boolean) => {
				if (!onlyIfMissing || !(key in server)) server[key] = structuredClone(value);
				return { value: structuredClone(server[key]) };
			})
		};
		const syncA = new PreferenceSync(transport);
		const syncB = new PreferenceSync(transport);
		const a = createTaskChecks(syncA);
		const b = createTaskChecks(syncB);
		syncA.start(false, 'account:A');
		syncB.start(false, 'account:A');
		await syncA.reconnect();
		await syncB.reconnect();

		const key = taskMessageKey('s1', '- [ ] one\n- [x] two');
		a.setChecked(key, 0, true);
		a.setChecked(key, 1, false);
		await vi.waitFor(() => expect(server.chat_task_checks).toEqual({ [key]: { 0: true, 1: false } }));
		await syncB.refresh('chat_task_checks');
		expect(b.checked(key, 0)).toBe(true);
		expect(b.checked(key, 1)).toBe(false);
		expect(b.checked(key, 2)).toBeUndefined();
	});

	it('survives a reload through the local cache', () => {
		const sync = { register: vi.fn(), save: vi.fn() };
		createTaskChecks(sync, localStorage).setChecked('s1:abc', 3, true);
		expect(createTaskChecks(sync, localStorage).checked('s1:abc', 3)).toBe(true);
	});

	it('keeps only the most recently ticked messages', () => {
		const checks = createTaskChecks({ register: vi.fn(), save: vi.fn() });
		for (let i = 0; i <= MAX_TASK_MESSAGES; i++) checks.setChecked(`s:${i}`, 0, true);
		checks.setChecked('s:1', 1, true); // touched again: moves to the newest end
		expect(Object.keys(checks.store.current)).toHaveLength(MAX_TASK_MESSAGES);
		expect(checks.checked('s:0', 0)).toBeUndefined();
		expect(checks.checked('s:1', 1)).toBe(true);
		expect(checks.checked(`s:${MAX_TASK_MESSAGES}`, 0)).toBe(true);
	});

	it('ignores malformed account values', () => {
		let apply: ((raw: unknown) => void) | undefined;
		const checks = createTaskChecks({ register: (_key, adapter) => { apply = adapter.apply; }, save: vi.fn() });
		apply!({ good: { 0: true, x: true, 1: 'yes' }, bad: [1], empty: {} });
		expect(checks.store.current).toEqual({ good: { 0: true } });
	});
});
