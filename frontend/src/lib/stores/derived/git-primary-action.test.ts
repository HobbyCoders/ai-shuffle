import { describe, it, expect } from 'vitest';
import { computePrimaryAction, finishBlockedByMergeability, type PrimaryActionInput } from './git-primary-action';

function input(overrides: Partial<PrimaryActionInput> = {}): PrimaryActionInput {
	return {
		hasChanges: false,
		hasOpenPr: false,
		isMergeable: null,
		commitsAhead: 0,
		differsFromBase: false,
		isOnDefaultBranch: false,
		needsPull: false,
		...overrides
	};
}

describe('computePrimaryAction', () => {
	it('commit wins over everything when there are uncommitted changes', () => {
		expect(
			computePrimaryAction(input({ hasChanges: true, hasOpenPr: true }))
		).toBe('commit');
	});

	it('finish when an open PR exists on a clean tree', () => {
		expect(computePrimaryAction(input({ hasOpenPr: true }))).toBe('finish');
	});

	it('finish even when mergeable is unknown (null)', () => {
		expect(computePrimaryAction(input({ hasOpenPr: true, isMergeable: null }))).toBe('finish');
	});

	it('create-pr when clean, no PR, ahead of default branch', () => {
		expect(computePrimaryAction(input({ commitsAhead: 2 }))).toBe('create-pr');
	});

	it('create-pr when differsFromBase even at zero commits ahead', () => {
		expect(
			computePrimaryAction(input({ commitsAhead: 0, differsFromBase: true }))
		).toBe('create-pr');
	});

	it('clean on the default branch', () => {
		expect(computePrimaryAction(input({ commitsAhead: 3, isOnDefaultBranch: true }))).toBe('clean');
	});

	it('clean when behind upstream (pull first)', () => {
		expect(computePrimaryAction(input({ commitsAhead: 2, needsPull: true }))).toBe('clean');
	});

	it('clean when nothing to do', () => {
		expect(computePrimaryAction(input())).toBe('clean');
	});
});

describe('finishBlockedByMergeability', () => {
	it('blocks only on an explicit false', () => {
		expect(finishBlockedByMergeability(false)).toBe(true);
		expect(finishBlockedByMergeability(null)).toBe(false);
		expect(finishBlockedByMergeability(true)).toBe(false);
	});
});
