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
import { evaluateSubmitGuards, gateBlocksComposer, projectRulesLane, sendButtonState, NO_CREDENTIAL_REASON, type SubmitGuardInput } from './submit-guards';
import { attachmentBlockReason } from './attach-disable';

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

	// The chat and voice lanes clear the project slice on every new compose and
	// render no picker, so the project guard there would refuse every send with
	// an instruction the user cannot carry out. Regression pin: chat mode was the
	// lane the attachment work shipped into, and a blocked send makes attaching
	// a file pointless.
	it('lets a projectless chat-lane send proceed', () => {
		expect(evaluateSubmitGuards(ready({ hasProject: false, mode: 'chat' }))).toBeNull();
	});

	it('lets a projectless voice-lane send proceed', () => {
		expect(evaluateSubmitGuards(ready({ hasProject: false, mode: 'voice' }))).toBeNull();
	});

	it('still names the missing project in the code lane', () => {
		const block = evaluateSubmitGuards(ready({ hasProject: false, mode: 'code' }));
		expect(block!.notice).toEqual({ message: 'Select a project first.', tone: 'warning' });
	});

	// A projectless lane skips only the project guard; everything ahead of it in
	// the chain still applies.
	it('still blocks a chat-lane send with no agent selected', () => {
		const block = evaluateSubmitGuards(ready({ hasProject: false, hasProfile: false, mode: 'chat' }));
		expect(block!.notice?.message).toMatch(/agent/i);
	});
});

describe('sendButtonState', () => {
	const ready = { hasContent: true, hasCredential: true, isUploading: false, isRecording: false, isTranscribing: false };

	it('is enabled with a plain title when nothing blocks', () => {
		expect(sendButtonState(ready)).toEqual({ disabled: false, title: 'Send message' });
		expect(sendButtonState({ ...ready, autoBranch: true }).title).toBe('Branch will be auto-created from your message');
	});

	it('names the actual blocker, never "Send message", when disabled', () => {
		const cases: [Partial<typeof ready>, string][] = [
			[{ hasCredential: false }, NO_CREDENTIAL_REASON],
			[{ isUploading: true }, 'Uploading files...'],
			[{ isRecording: true }, 'Recording...'],
			[{ isTranscribing: true }, 'Transcribing...'],
			[{ hasContent: false }, 'Type a message to send']
		];
		for (const [patch, title] of cases) {
			expect(sendButtonState({ ...ready, ...patch })).toEqual({ disabled: true, title });
		}
	});

	it('puts the missing credential first, since nothing else can be fixed without it', () => {
		expect(sendButtonState({ ...ready, hasCredential: false, hasContent: false, isUploading: true }).title).toBe(NO_CREDENTIAL_REASON);
	});
});

describe('projectRulesLane', () => {
	const home: SubmitGuardInput = {
		hasPrompt: true, isUploading: false, isCreatingWorktree: false,
		hasProfile: true, hasAnyProfile: true, hasProject: false
	};

	it("lets the assistant's home send and attach without a project", () => {
		// The home is stored as a code session with no project. Read as a code
		// lane it refused every send with "Select a project first."
		expect(evaluateSubmitGuards({ ...home, mode: 'code' })!.notice?.message).toBe('Select a project first.');
		const lane = projectRulesLane('code', true);
		expect(lane).toBe('chat');
		expect(evaluateSubmitGuards({ ...home, mode: lane })).toBeNull();
		expect(attachmentBlockReason(null, null, lane, 'home-session')).toBeNull();
	});

	it('leaves ordinary sessions on their own lane', () => {
		expect(projectRulesLane('code', false)).toBe('code');
		expect(projectRulesLane('chat', false)).toBe('chat');
		expect(projectRulesLane(undefined, false)).toBe('code');
		expect(evaluateSubmitGuards({ ...home, mode: projectRulesLane('code', false) })).not.toBeNull();
	});
});

describe('gateBlocksComposer', () => {
	const gate = { ready: false, reason: 'Connect a provider', license_allows_runs: true };

	it('stays quiet when the gate is ready or absent', () => {
		expect(gateBlocksComposer(null, false)).toBe(false);
		expect(gateBlocksComposer({ ...gate, ready: true }, false)).toBe(false);
		expect(gateBlocksComposer({ ...gate, reason: null }, false)).toBe(false);
	});

	it('warns when no provider is ready at all', () => {
		expect(gateBlocksComposer({ ...gate, any_provider_ready: false }, true)).toBe(true);
	});

	it('does not warn about one provider when another is ready', () => {
		// Claude not signed in, OpenAI connected: the app works.
		expect(gateBlocksComposer({ ...gate, any_provider_ready: true }, false)).toBe(false);
	});

	it('always warns when the license blocks runs', () => {
		expect(gateBlocksComposer({ ...gate, license_allows_runs: false, any_provider_ready: true }, true)).toBe(true);
	});

	it('falls back to the any-credential flag on older servers', () => {
		expect(gateBlocksComposer(gate, false)).toBe(true);
		expect(gateBlocksComposer(gate, true)).toBe(false);
	});
});
