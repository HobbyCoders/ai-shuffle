/**
 * Tests for Ship It button visibility.
 *
 * Regression guard for "don't show Ship It for branches with nothing to ship":
 * the button used to key off the raw commit-count-ahead, so a branch that was
 * ahead by commit count but carried a 0/0 net diff (empty/cancelling commits, or
 * a cleaned-up worktree already merged into the base) kept the button visible.
 * The gate now requires real content — uncommitted changes OR a non-empty
 * base...HEAD diff (`branchDiffersFromBase`) — AND a confirmed repo
 * (`isRepoConfirmed`), so an empty/cleaned-up worktree folder never shows it.
 */

import { describe, it, expect } from 'vitest';
import { computeShowShipIt, type ShipItVisibilityInput } from './ship-it-visibility';

function input(overrides: Partial<ShipItVisibilityInput> = {}): ShipItVisibilityInput {
	return {
		isRepoConfirmed: true,
		hasChanges: false,
		branchDiffersFromBase: false,
		isOnDefaultBranch: false,
		needsPull: false,
		hasShipItHandler: true,
		...overrides
	};
}

describe('computeShowShipIt', () => {
	it('shows when there are uncommitted working-tree changes', () => {
		expect(computeShowShipIt(input({ hasChanges: true }))).toBe(true);
	});

	it('shows when the branch has real committed content over its base', () => {
		expect(computeShowShipIt(input({ branchDiffersFromBase: true }))).toBe(true);
	});

	it('HIDES a branch that is ahead by commit count but has a 0/0 net diff', () => {
		// The core bug: commits ahead but nothing to ship (empty/cancelling commits,
		// or an already-merged/cleaned-up worktree). hasChanges + differs both false.
		expect(computeShowShipIt(input())).toBe(false);
	});

	it('hides on a clean default branch', () => {
		expect(computeShowShipIt(input({ isOnDefaultBranch: true }))).toBe(false);
	});

	it('hides on the default branch even with uncommitted changes', () => {
		// Nothing to "ship" from default — Commit handles that flow instead.
		expect(
			computeShowShipIt(input({ isOnDefaultBranch: true, hasChanges: true }))
		).toBe(false);
	});

	it('hides when the branch is behind upstream and must pull first', () => {
		expect(
			computeShowShipIt(input({ branchDiffersFromBase: true, needsPull: true }))
		).toBe(false);
	});

	it('hides when no Ship It handler is wired up', () => {
		expect(
			computeShowShipIt(input({ hasChanges: true, hasShipItHandler: false }))
		).toBe(false);
	});

	it('HIDES for an empty / cleaned-up worktree folder that is not a confirmed repo', () => {
		// Reusable worktree sessions can leave the folder empty between uses. Even
		// if stale cached diffStats make hasChanges look true, Ship It must stay
		// hidden until status positively confirms a repo.
		expect(
			computeShowShipIt(input({ isRepoConfirmed: false, hasChanges: true }))
		).toBe(false);
		expect(
			computeShowShipIt(input({ isRepoConfirmed: false, branchDiffersFromBase: true }))
		).toBe(false);
	});

	it('hides while repo status is unconfirmed even with everything else shippable', () => {
		expect(
			computeShowShipIt(
				input({ isRepoConfirmed: false, hasChanges: true, branchDiffersFromBase: true })
			)
		).toBe(false);
	});

	it('shows when both uncommitted changes and committed content exist', () => {
		expect(
			computeShowShipIt(input({ hasChanges: true, branchDiffersFromBase: true }))
		).toBe(true);
	});
});
