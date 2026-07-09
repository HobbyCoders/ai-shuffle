/**
 * Tests for gitActionPreferences — specifically the "strip unknown keys" merge
 * behavior added when the canned-prompt system was removed.
 *
 * The old system persisted a `customMessages` blob inside the shared
 * `git_action_settings` preference. After its removal `mergePreferences` must
 * rebuild state from ONLY the known structured keys, so stale `customMessages`
 * (and any other unknown fields) can't ride along into state or get re-saved.
 *
 * The store caches its state at module scope and reads localStorage once at
 * import, so we seed storage then re-import fresh (vi.resetModules).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/api/client', () => ({
	getPreference: vi.fn().mockResolvedValue(null),
	setPreference: vi.fn().mockResolvedValue(undefined),
}));

const STORAGE_KEY = 'ai-shuffle-git-action-preferences';

beforeEach(() => {
	localStorage.clear();
	vi.resetModules();
});

async function freshStore() {
	return (await import('./gitActionPreferences.svelte')).gitActionPreferences;
}

describe('gitActionPreferences — merge strips unknown keys', () => {
	it('drops stale customMessages and unknown fields from persisted prefs', async () => {
		localStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({
				includeUnstaged: false,
				nextStep: 'pr',
				// Stale blob from the removed canned-prompt system + junk key.
				customMessages: { commit: 'do the thing', shipIt: 'YOLO' },
				bogusKey: 42,
			})
		);

		const store = await freshStore();
		const cur = store.current as unknown as Record<string, unknown>;

		// Known keys survive.
		expect(cur.includeUnstaged).toBe(false);
		expect(cur.nextStep).toBe('pr');
		// Untouched keys fall back to defaults.
		expect(cur.mergeMethod).toBe('squash');
		expect(cur.pullStrategy).toBe('rebase');
		// Stale / unknown keys are stripped entirely.
		expect('customMessages' in cur).toBe(false);
		expect('bogusKey' in cur).toBe(false);
	});

	it('produces a clean default set when nothing is persisted', async () => {
		const store = await freshStore();
		const cur = store.current as unknown as Record<string, unknown>;
		expect(Object.keys(cur).sort()).toEqual(
			['autoStash', 'cleanupAfterMerge', 'includeUnstaged', 'mergeMethod', 'nextStep', 'pullStrategy'].sort()
		);
	});
});
