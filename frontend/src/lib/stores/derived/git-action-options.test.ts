/**
 * Unit tests for the pure git-action-option derivations.
 *
 * computeShipItSteps drives the read-only "what will happen" preview in the
 * Ship It option popover, so its branching (dirty tree → Commit step, missing PR
 * → Create PR step, cleanup preference → Clean up step) must stay honest.
 */
import { describe, it, expect } from 'vitest';
import { computeShipItSteps } from './git-action-options';

const ids = (input: Parameters<typeof computeShipItSteps>[0]) =>
	computeShipItSteps(input).map((s) => s.id);

describe('computeShipItSteps', () => {
	it('shows the full pipeline for a dirty branch with no PR and cleanup on', () => {
		expect(ids({ hasChanges: true, hasOpenPR: false, cleanup: true })).toEqual([
			'commit',
			'push',
			'pr',
			'merge',
			'cleanup'
		]);
	});

	it('drops the commit step when the tree is clean', () => {
		expect(ids({ hasChanges: false, hasOpenPR: false, cleanup: true })).toEqual([
			'push',
			'pr',
			'merge',
			'cleanup'
		]);
	});

	it('drops the create-PR step when a PR already exists', () => {
		expect(ids({ hasChanges: true, hasOpenPR: true, cleanup: true })).toEqual([
			'commit',
			'push',
			'merge',
			'cleanup'
		]);
	});

	it('drops the cleanup step when the cleanup preference is off', () => {
		expect(ids({ hasChanges: true, hasOpenPR: false, cleanup: false })).toEqual([
			'commit',
			'push',
			'pr',
			'merge'
		]);
	});

	it('always keeps push and merge (minimal clean + existing-PR + no-cleanup case)', () => {
		expect(ids({ hasChanges: false, hasOpenPR: true, cleanup: false })).toEqual([
			'push',
			'merge'
		]);
	});

	it('gives every step a human label', () => {
		const steps = computeShipItSteps({ hasChanges: true, hasOpenPR: false, cleanup: true });
		for (const s of steps) {
			expect(s.label.length).toBeGreaterThan(0);
		}
	});
});
