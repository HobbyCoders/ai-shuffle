/**
 * Tests for DiffViewer behavior logic.
 *
 * These test the extracted helper functions that DiffViewer uses
 * to determine when to re-fetch diffs and which mode to render.
 */

import { describe, it, expect } from 'vitest';

// ── shouldRefetchDiff ────────────────────────────────────────────────────
// This function should exist to determine if a diff needs re-fetching.
// It prevents flashing during streaming by comparing filenames, not object refs.

describe('shouldRefetchDiff', () => {
	it('returns true when filename changes', async () => {
		const { shouldRefetchDiff } = await import('../../utils/diff-parser');
		const oldFile = { file: 'src/foo.ts', status: 'M', category: 'modified', additions: 1, deletions: 0, is_binary: false };
		const newFile = { file: 'src/bar.ts', status: 'M', category: 'modified', additions: 2, deletions: 1, is_binary: false };
		expect(shouldRefetchDiff(oldFile, newFile)).toBe(true);
	});

	it('returns false when same filename with different object reference', async () => {
		const { shouldRefetchDiff } = await import('../../utils/diff-parser');
		const oldFile = { file: 'src/foo.ts', status: 'M', category: 'modified', additions: 1, deletions: 0, is_binary: false };
		const newFile = { file: 'src/foo.ts', status: 'M', category: 'modified', additions: 3, deletions: 1, is_binary: false };
		expect(shouldRefetchDiff(oldFile, newFile)).toBe(false);
	});

	it('returns true when previous file is null', async () => {
		const { shouldRefetchDiff } = await import('../../utils/diff-parser');
		const newFile = { file: 'src/foo.ts', status: 'M', category: 'modified', additions: 1, deletions: 0, is_binary: false };
		expect(shouldRefetchDiff(null, newFile)).toBe(true);
	});

	it('returns false when both are null', async () => {
		const { shouldRefetchDiff } = await import('../../utils/diff-parser');
		expect(shouldRefetchDiff(null, null)).toBe(false);
	});

	it('returns true when category changes (staged vs modified affects diff content)', async () => {
		const { shouldRefetchDiff } = await import('../../utils/diff-parser');
		const oldFile = { file: 'src/foo.ts', status: 'M', category: 'modified', additions: 1, deletions: 0, is_binary: false };
		const newFile = { file: 'src/foo.ts', status: 'M', category: 'staged', additions: 1, deletions: 0, is_binary: false };
		expect(shouldRefetchDiff(oldFile, newFile)).toBe(true);
	});
});
