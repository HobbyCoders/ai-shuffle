import { describe, expect, it, vi, beforeEach } from 'vitest';

vi.mock('$lib/api/pr-watches', () => ({
	listPrWatches: vi.fn(),
	startPrWatch: vi.fn(),
	stopPrWatch: vi.fn()
}));

import * as api from '$lib/api/pr-watches';
import type { PrWatch } from '$lib/api/pr-watches';
import { prWatches, repoFromPrUrl, watchKey, watchSummary } from './pr-watches.svelte';
import { parseBots } from '$lib/components/settings/pages/pr-watch-settings.svelte';

function watch(over: Partial<PrWatch> = {}): PrWatch {
	return {
		id: 'w1', repo: 'Acme/Widgets', pr_number: 7, pr_url: null, pr_title: null,
		session_id: 's1', status: 'active', reason: null, wakes_today: 0, spent_today_usd: 0,
		needs_owner: false, pending: 0, last_error: null, last_wake_at: null,
		created_at: '2026-11-23T10:00:00Z', updated_at: null, ...over
	};
}

describe('repoFromPrUrl', () => {
	it('reads owner/name from a pull request link', () => {
		expect(repoFromPrUrl('https://github.com/Acme/Widgets/pull/7')).toBe('Acme/Widgets');
		expect(repoFromPrUrl('https://github.com/Acme/Widgets/pull/7/files')).toBe('Acme/Widgets');
	});
	it('refuses anything that is not a GitHub pull request link', () => {
		expect(repoFromPrUrl('https://example.com/Acme/Widgets/pull/7')).toBeNull();
		expect(repoFromPrUrl('https://github.com/Acme/Widgets/issues/7')).toBeNull();
		expect(repoFromPrUrl(undefined)).toBeNull();
	});
});

describe('watchSummary', () => {
	it('says what the watch is doing', () => {
		expect(watchSummary(null)).toMatch(/wakes this chat/);
		expect(watchSummary(watch())).toBe('watching CI, reviews and conflicts');
		expect(watchSummary(watch({ wakes_today: 2 }))).toBe('watching · 2 wakes today');
		expect(watchSummary(watch({ needs_owner: true }))).toMatch(/needs you/);
		expect(watchSummary(watch({ session_id: null }))).toMatch(/notifies you only/);
		expect(watchSummary(watch({ last_error: 'gh is not signed in' }))).toMatch(/gh is not signed in/);
	});
});

describe('parseBots', () => {
	it('normalizes logins and drops duplicates', () => {
		expect(parseBots('coderabbitai[bot], @Copilot  copilot,,')).toEqual(['coderabbitai', 'copilot']);
		expect(parseBots('')).toEqual([]);
	});
});

describe('prWatches store', () => {
	beforeEach(() => vi.clearAllMocks());

	it('keys watches case-insensitively and lets an active watch win over an ended one', async () => {
		vi.mocked(api.listPrWatches).mockResolvedValue({
			watches: [
				watch({ id: 'new', created_at: '2026-11-23T12:00:00Z' }),
				watch({ id: 'old', status: 'ended', created_at: '2026-11-22T12:00:00Z' })
			]
		});
		await prWatches.load(true);
		expect(prWatches.active('acme/widgets', 7)?.id).toBe('new');
		expect(prWatches.active('acme/widgets', 8)).toBeNull();
	});

	it('starts a watch, then stops the same one', async () => {
		vi.mocked(api.listPrWatches).mockResolvedValue({ watches: [] });
		await prWatches.load(true);
		vi.mocked(api.startPrWatch).mockResolvedValue(watch({ id: 'w9' }));
		await prWatches.toggle({ repo: 'Acme/Widgets', number: 7, sessionId: 's1' });
		expect(api.startPrWatch).toHaveBeenCalledWith(expect.objectContaining({ repo: 'Acme/Widgets', pr_number: 7, session_id: 's1' }));
		expect(prWatches.active('Acme/Widgets', 7)?.id).toBe('w9');

		vi.mocked(api.stopPrWatch).mockResolvedValue(watch({ id: 'w9', status: 'stopped' }));
		await prWatches.toggle({ repo: 'Acme/Widgets', number: 7 });
		expect(api.stopPrWatch).toHaveBeenCalledWith('w9');
		expect(prWatches.active('Acme/Widgets', 7)).toBeNull();
	});

	it('shows the server error and keeps the old state', async () => {
		vi.mocked(api.listPrWatches).mockResolvedValue({ watches: [] });
		await prWatches.load(true);
		vi.mocked(api.startPrWatch).mockRejectedValue({ detail: 'That chat no longer exists' });
		await prWatches.toggle({ repo: 'Acme/Widgets', number: 7 });
		expect(prWatches.error[watchKey('Acme/Widgets', 7)]).toBe('That chat no longer exists');
		expect(prWatches.active('Acme/Widgets', 7)).toBeNull();
	});
});
