/**
 * The composer's circle rules: Talk turns into Send, Stop follows while work
 * runs, and the text wraps the buttons below it only when it needs a second
 * line.
 */
import { describe, expect, it } from 'vitest';
import { composerCircles, needsWrap } from './composer-wrap';

const idle = { callAvailable: true, hasContent: false, working: false, busy: false };

describe('composerCircles (Talk turns into Send)', () => {
	it('shows Talk while there is nothing to send and a call can start', () => {
		expect(composerCircles(idle)).toEqual(['talk']);
	});

	it('turns Talk into Send once there is sendable content', () => {
		expect(composerCircles({ ...idle, hasContent: true })).toEqual(['send']);
	});

	it('falls back to the resting Send where no call can start', () => {
		expect(composerCircles({ ...idle, callAvailable: false })).toEqual(['send']);
	});

	it('keeps Talk and adds Stop as the last circle while work runs with no draft', () => {
		expect(composerCircles({ ...idle, working: true })).toEqual(['talk', 'stop']);
	});

	it('shows Send then Stop while work runs with a draft (Send queues it)', () => {
		expect(composerCircles({ ...idle, working: true, hasContent: true })).toEqual(['send', 'stop']);
	});

	it('shows only Stop while work runs with no draft and no call', () => {
		expect(composerCircles({ ...idle, working: true, callAvailable: false })).toEqual(['stop']);
	});

	it('gives the slot to the spinner while a worktree is created or a send waits', () => {
		expect(composerCircles({ ...idle, busy: true })).toEqual(['busy']);
		expect(composerCircles({ ...idle, busy: true, hasContent: true, working: true })).toEqual(['busy']);
	});
});

describe('needsWrap', () => {
	it('keeps one line inline', () => {
		expect(needsWrap('Fix the tests', 1)).toBe(false);
	});

	it('wraps once the text needs a second line at the single-line width', () => {
		expect(needsWrap('A long request that runs past the edge', 2)).toBe(true);
	});

	it('wraps for a line break, including one the caret has just made', () => {
		expect(needsWrap('first\nsecond', 1)).toBe(true);
		expect(needsWrap('first line\n', 1)).toBe(true);
		expect(needsWrap('one line   ', 1)).toBe(false);
	});

	it('never wraps an empty composer', () => {
		expect(needsWrap('', 3)).toBe(false);
		expect(needsWrap('   \n ', 3)).toBe(false);
	});
});
