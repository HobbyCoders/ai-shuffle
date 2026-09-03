/**
 * Tests for sessionMode — the sidepanel's code/chat/voice lane.
 *
 * Two things matter here. First, `normalizeSessionMode` is the boundary every
 * untrusted value crosses: a `sessions.mode` column that is NULL for every
 * pre-modes row, a localStorage string a user could have hand-edited, a value
 * from an older client. All of it must land on a real mode, and NULL must land
 * on 'code' specifically — that is the storage contract, not a fallback.
 *
 * Second, the pick has to survive a reload, so the round-trip is asserted
 * against a freshly-imported module rather than against the live store object.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

async function freshStore() {
	vi.resetModules();
	return import('./sessionMode.svelte');
}

beforeEach(() => {
	localStorage.clear();
	vi.resetModules();
});

describe('normalizeSessionMode', () => {
	it('resolves absent values to code — NULL is how a code session is stored', async () => {
		const { normalizeSessionMode } = await freshStore();
		expect(normalizeSessionMode(null)).toBe('code');
		expect(normalizeSessionMode(undefined)).toBe('code');
		expect(normalizeSessionMode('')).toBe('code');
	});

	it('passes the three real modes through', async () => {
		const { normalizeSessionMode } = await freshStore();
		expect(normalizeSessionMode('code')).toBe('code');
		expect(normalizeSessionMode('chat')).toBe('chat');
		expect(normalizeSessionMode('voice')).toBe('voice');
	});

	it('tolerates whitespace and casing', async () => {
		const { normalizeSessionMode } = await freshStore();
		expect(normalizeSessionMode(' chat ')).toBe('chat');
		expect(normalizeSessionMode('CHAT')).toBe('chat');
		expect(normalizeSessionMode('\tVoice\n')).toBe('voice');
	});

	it('falls back to code for unknown strings and non-strings', async () => {
		const { normalizeSessionMode } = await freshStore();
		expect(normalizeSessionMode('nonsense')).toBe('code');
		expect(normalizeSessionMode(123)).toBe('code');
		expect(normalizeSessionMode({})).toBe('code');
		expect(normalizeSessionMode(true)).toBe('code');
	});
});

describe('SESSION_MODES / DEFAULT_SESSION_MODE', () => {
	it('lists the modes in switcher order with code first', async () => {
		const { SESSION_MODES, DEFAULT_SESSION_MODE } = await freshStore();
		expect(SESSION_MODES).toEqual(['code', 'chat', 'voice']);
		expect(DEFAULT_SESSION_MODE).toBe('code');
	});
});

describe('appMode persistence', () => {
	it('defaults to code with nothing stored', async () => {
		const { appMode } = await freshStore();
		expect(appMode.current).toBe('code');
	});

	it('writes the pick to localStorage and reads it back on a fresh load', async () => {
		const first = await freshStore();
		first.appMode.set('voice');
		expect(first.appMode.current).toBe('voice');
		expect(localStorage.getItem('ai-shuffle.sessionMode')).toBe('voice');

		const second = await freshStore();
		expect(second.appMode.current).toBe('voice');
	});

	it('hydrates a garbage stored value as code without clearing it', async () => {
		localStorage.setItem('ai-shuffle.sessionMode', 'wat');
		const { appMode } = await freshStore();
		expect(appMode.current).toBe('code');
	});

	it('notifies subscribers on a real change only', async () => {
		const { appMode } = await freshStore();
		const seen: string[] = [];
		const stop = appMode.subscribe((m) => seen.push(m));

		appMode.set('chat');
		appMode.set('chat');
		appMode.set('code');
		stop();
		appMode.set('voice');

		// Initial value, then one entry per distinct transition; the repeat and
		// the post-unsubscribe write contribute nothing.
		expect(seen).toEqual(['code', 'chat', 'code']);
	});

	it('exposes the derived store for `$sessionMode` in components', async () => {
		const { appMode, sessionMode } = await freshStore();
		const seen: string[] = [];
		const stop = sessionMode.subscribe((m) => seen.push(m));

		appMode.set('chat');
		expect(sessionMode.current).toBe('chat');
		stop();

		expect(seen).toEqual(['code', 'chat']);
	});
});
