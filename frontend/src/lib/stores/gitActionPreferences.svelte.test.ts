/**
 * Tests for gitActionPreferences — the merge behavior for the shared
 * `git_action_settings` server blob.
 *
 * The removed canned-prompt system persisted a `customMessages` blob inside this
 * preference; `mergePreferences` must still strip that known-dead key. But it
 * must PRESERVE any other unknown/future keys: the blob is shared cross-version,
 * so an older app must not wipe a newer app's keys on every save (a whitelist
 * rebuild would silently downgrade the shared blob).
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

describe('gitActionPreferences — merge strips customMessages, preserves unknown keys', () => {
	it('drops stale customMessages but preserves unknown/future keys', async () => {
		localStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({
				includeUnstaged: false,
				nextStep: 'pr',
				// Stale blob from the removed canned-prompt system — must be dropped.
				customMessages: { commit: 'do the thing', shipIt: 'YOLO' },
				// A key a NEWER app version added — must survive an older app's save.
				futureFeatureKey: 42,
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
		// The known-dead customMessages key is stripped.
		expect('customMessages' in cur).toBe(false);
		// Unknown/future keys are preserved (forward-compat with the shared blob).
		expect('futureFeatureKey' in cur).toBe(true);
		expect(cur.futureFeatureKey).toBe(42);
	});

	it('produces a clean default set when nothing is persisted', async () => {
		const store = await freshStore();
		const cur = store.current as unknown as Record<string, unknown>;
		expect(Object.keys(cur).sort()).toEqual(
			['autoStash', 'cleanupAfterMerge', 'includeUnstaged', 'mergeMethod', 'nextStep', 'pullStrategy'].sort()
		);
	});
});
