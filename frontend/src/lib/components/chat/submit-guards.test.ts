/**
 * Behavior tests for the ChatInput submit guards.
 *
 * The bug (observed live 2026-07-10): clicking Send / hitting Enter with a
 * missing precondition (no project, no profile, …) silently early-returned — the
 * message stayed in the composer and nothing happened, reading as a dead app.
 * These tests pin that every blockable-but-plausible state now yields a
 * user-facing notice, while a genuinely-ready send proceeds and an empty
 * composer stays silent.
 */
import { describe, it, expect } from 'vitest';
import { evaluateSubmitGuards, type SubmitGuardInput } from './submit-guards';

/** A fully-ready send: prompt typed, nothing uploading, profile + project set. */
function ready(over: Partial<SubmitGuardInput> = {}): SubmitGuardInput {
	return {
		hasPrompt: true,
		isUploading: false,
		isCreatingWorktree: false,
		hasProfile: true,
		hasAnyProfile: true,
		hasProject: true,
		...over
	};
}

describe('evaluateSubmitGuards', () => {
	it('lets a fully-ready send proceed (returns null)', () => {
		expect(evaluateSubmitGuards(ready())).toBeNull();
	});

	it('blocks an empty composer silently (no toast)', () => {
		const block = evaluateSubmitGuards(ready({ hasPrompt: false }));
		expect(block).not.toBeNull();
		expect(block!.notice).toBeNull();
	});

	it('surfaces a warning while a file upload is in flight', () => {
		const block = evaluateSubmitGuards(ready({ isUploading: true }));
		expect(block!.notice).toEqual({
			message: 'Wait for the file upload to finish, then send.',
			tone: 'warning'
		});
	});

	it('surfaces an info notice while the worktree is being created', () => {
		const block = evaluateSubmitGuards(ready({ isCreatingWorktree: true }));
		expect(block!.notice?.tone).toBe('info');
		expect(block!.notice?.message).toMatch(/creating the worktree/i);
	});

	it('names the missing agent when there is one to pick', () => {
		const block = evaluateSubmitGuards(ready({ hasProfile: false }));
		expect(block!.notice).toEqual({ message: 'Select an agent first.', tone: 'warning' });
	});

	it('tells a fresh install to create an agent rather than pick one', () => {
		// The app seeds no agents, so "select an agent" would be advice the user
		// cannot act on — there is nothing in the picker.
		const block = evaluateSubmitGuards(ready({ hasProfile: false, hasAnyProfile: false }));
		expect(block!.notice).toEqual({
			message: 'No agents yet — create one to start chatting.',
			tone: 'warning'
		});
	});

	it('names the missing project', () => {
		const block = evaluateSubmitGuards(ready({ hasProject: false }));
		expect(block!.notice).toEqual({ message: 'Select a project first.', tone: 'warning' });
	});

	it('checks the empty composer before every other guard (silent wins)', () => {
		// Nothing typed AND no project: the composer being empty is the reason to
		// stop, and we must not nag about a project the user has not tried to use yet.
		const block = evaluateSubmitGuards(ready({ hasPrompt: false, hasProject: false, hasProfile: false }));
		expect(block!.notice).toBeNull();
	});

	it('prefers the upload notice over missing profile/project', () => {
		// A ready-to-send prompt with an upload still in flight AND no project:
		// the upload is the immediate blocker the user should hear about first.
		const block = evaluateSubmitGuards(
			ready({ isUploading: true, hasProfile: false, hasProject: false })
		);
		expect(block!.notice?.message).toMatch(/upload/i);
	});

	it('prefers the agent notice over the project notice', () => {
		const block = evaluateSubmitGuards(ready({ hasProfile: false, hasProject: false }));
		expect(block!.notice?.message).toMatch(/agent/i);
	});
});
