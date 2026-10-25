import { describe, expect, it } from 'vitest';
import type { FileChangeInfo } from '$lib/api/git';
import { baseScopeUnavailableReason, errorMessage, filterWorkingFiles, originLabel, statusLabel } from './scope';

const row = (file: string, category: string, status = 'M'): FileChangeInfo =>
	({ file, category, status, additions: 1, deletions: 0, is_binary: false });
const files = [row('a.ts', 'staged'), row('a.ts', 'modified'), row('b.txt', 'untracked', '?')];

describe('review scope rules', () => {
	it('filters the uncommitted scope', () => {
		expect(filterWorkingFiles(files, 'all')).toHaveLength(3);
		expect(filterWorkingFiles(files, 'staged').map(f => f.category)).toEqual(['staged']);
		expect(filterWorkingFiles(files, 'unstaged').map(f => f.category)).toEqual(['modified', 'untracked']);
	});
	it('needs a chat session for changes vs base', () => {
		expect(baseScopeUnavailableReason('session-1')).toBeNull();
		expect(baseScopeUnavailableReason(null)).toBeTruthy();
		expect(baseScopeUnavailableReason(undefined)).toBeTruthy();
	});
	it('labels base rows by where the change lives', () => {
		expect(originLabel('committed')).toBe('Committed');
		expect(originLabel('uncommitted')).toBe('Uncommitted');
		expect(originLabel('both')).toBe('Committed + local');
		expect(originLabel(undefined)).toBe('');
	});
	it('keeps status words for working rows and stays quiet for unknown base ones', () => {
		expect(statusLabel(row('a', 'staged'))).toBe('Staged');
		expect(statusLabel(row('a', 'untracked', '?'))).toBe('New');
		expect(statusLabel(row('a', 'base', 'D'))).toBe('Deleted');
		expect(statusLabel(row('a', 'base', 'T'))).toBe('');
	});
	it('prefers the API detail for errors', () => {
		expect(errorMessage({ detail: 'Detached HEAD' }, 'x')).toBe('Detached HEAD');
		expect(errorMessage(new Error('boom'), 'x')).toBe('boom');
		expect(errorMessage(null, 'fallback')).toBe('fallback');
	});
});
