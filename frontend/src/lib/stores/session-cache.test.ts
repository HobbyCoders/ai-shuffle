/**
 * Session ephemeral cache — LRU of per-session composer state that survives
 * session switches (drafts, scroll, run overrides, worktree config).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { sessionEphemeralCache as cache, type SessionEphemeral } from './session-cache';

function entry(draft: string, extra: Partial<SessionEphemeral> = {}): SessionEphemeral {
	return {
		title: 't', draft, scrollTop: 0, project: 'p', projectV2Id: null, modelOverride: null,
		permissionModeOverride: null, effortLevelOverride: null, worktreeIntent: 'local' as SessionEphemeral['worktreeIntent'],
		deviceIntent: null, worktreeId: null, worktreeBranch: '', worktreeBaseBranch: '',
		selectedWorktreeBranch: null, worktreeMode: 'new', worktreeDeleted: false, ...extra
	};
}

beforeEach(() => cache.clear());

describe('sessionEphemeralCache', () => {
	it('returns null for an unknown session', () => {
		expect(cache.restore('nope')).toBeNull();
		expect(cache.has('nope')).toBe(false);
	});

	it('saves and restores per session', () => {
		cache.save('a', entry('draft a', { scrollTop: 120 }));
		cache.save('b', entry('draft b'));
		expect(cache.restore('a')).toMatchObject({ draft: 'draft a', scrollTop: 120 });
		expect(cache.restore('b')?.draft).toBe('draft b');
	});

	it('a re-save replaces the entry', () => {
		cache.save('a', entry('one'));
		cache.save('a', entry('two'));
		expect(cache.restore('a')?.draft).toBe('two');
	});

	it('evicts the least recently used entry beyond 50', () => {
		for (let i = 0; i < 50; i++) cache.save(`s${i}`, entry(String(i)));
		// Touch s0 so s1 becomes the oldest.
		cache.restore('s0');
		cache.save('s50', entry('50'));
		expect(cache.has('s0')).toBe(true);
		expect(cache.has('s1')).toBe(false);
		expect(cache.has('s50')).toBe(true);
		// Re-saving an existing key does not evict anything.
		cache.save('s2', entry('2b'));
		expect(cache.has('s3')).toBe(true);
	});

	it('patchDraft updates a cached draft and never creates an entry', () => {
		cache.save('a', entry('stale'));
		cache.patchDraft('a', 'live text');
		expect(cache.restore('a')?.draft).toBe('live text');
		cache.patchDraft('ghost', 'leak');
		expect(cache.has('ghost')).toBe(false);
	});

	it('remove and clear drop entries', () => {
		cache.save('a', entry('a'));
		cache.save('b', entry('b'));
		cache.remove('a');
		expect(cache.has('a')).toBe(false);
		cache.clear();
		expect(cache.has('b')).toBe(false);
	});
});
