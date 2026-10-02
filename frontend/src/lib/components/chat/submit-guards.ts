/**
 * Precondition guards for sending a chat message.
 *
 * These mirror the early-return checks that used to live inline in
 * `ChatInput.handleSubmit`. Pulling them out keeps a single, testable source of
 * truth for *why* a send is blocked — so the UI can surface the reason with a
 * toast instead of silently swallowing the click (which read as a dead app).
 *
 * The order matters and is preserved exactly from the original inline guards:
 *   empty composer → upload in flight → worktree being created → no agent → no project.
 */
import type { NotificationSeverity } from '$lib/stores/notifications';
import type { SessionMode } from '$lib/stores/sessionMode.svelte';

/** A user-facing reason a send was blocked, or `null` for a silent block. */
export interface SubmitBlock {
	/**
	 * The toast to surface. `null` means block silently: the composer is empty,
	 * so there is nothing to send and no cue is warranted.
	 */
	notice: { message: string; tone: NotificationSeverity } | null;
}

/** The slice of ChatInput state the guards need. */
export interface SubmitGuardInput {
	/** Whether the composer has non-whitespace content to send. */
	hasPrompt: boolean;
	/** A file upload is in flight. */
	isUploading: boolean;
	/** A worktree is being auto-created from the pending message. */
	isCreatingWorktree: boolean;
	/** A profile is selected. */
	hasProfile: boolean;
	/**
	 * Any agent exists to select. The app seeds none, so "pick an agent" is
	 * advice the user cannot follow on a fresh install — they have to make one
	 * first, and the toast has to say so.
	 */
	hasAnyProfile: boolean;
	/** A project is selected. */
	hasProject: boolean;
	/**
	 * The lane this composer belongs to. Only the code lane has a project to
	 * select, so only the code lane can be blocked for the lack of one — see the
	 * project guard below. Defaults to 'code' so the pre-lanes callers keep their
	 * behaviour.
	 */
	mode?: SessionMode;
}

/**
 * Returns why a send is blocked, or `null` when the message may proceed.
 * When a `SubmitBlock` is returned, the caller keeps the message in the composer
 * and (unless `notice` is null) shows the toast so the user knows what's missing.
 */
export function evaluateSubmitGuards(s: SubmitGuardInput): SubmitBlock | null {
	// Nothing typed — nothing to send, and no cue is needed (composer is empty).
	if (!s.hasPrompt) return { notice: null };

	if (s.isUploading) {
		return { notice: { message: 'Wait for the file upload to finish, then send.', tone: 'warning' } };
	}

	// The send button is disabled while a worktree is being created, but the
	// Enter-key path reaches handleSubmit directly. Without this the send would
	// fall through to the plain query path (no worktree_id) and run in the main
	// checkout — so we block and tell the user why.
	if (s.isCreatingWorktree) {
		return { notice: { message: 'Still creating the worktree — hang on a moment.', tone: 'info' } };
	}

	if (!s.hasProfile) {
		return s.hasAnyProfile
			? { notice: { message: 'Select an agent first.', tone: 'warning' } }
			: { notice: { message: 'No agents yet — create one to start chatting.', tone: 'warning' } };
	}
	// Only the code lane binds a repository. Chat and voice are projectless by
	// construction — `newChatModeState` clears the project slice for them and
	// neither surface renders a picker — so demanding a project there refuses
	// every send with advice the user has no control to follow.
	if (!s.hasProject && (s.mode ?? 'code') === 'code') {
		return { notice: { message: 'Select a project first.', tone: 'warning' } };
	}

	return null;
}

/** Why a credential-gated composer control is disabled. */
export const NO_CREDENTIAL_REASON = 'Connect a provider in Settings first';

/** What the send button's disabled state depends on. */
export interface SendButtonInput {
	hasContent: boolean;
	hasCredential: boolean;
	isUploading: boolean;
	isRecording: boolean;
	isTranscribing: boolean;
	/** The send will create a branch/worktree from the message. */
	autoBranch?: boolean;
}

/**
 * The send button's disabled state and the tooltip that names the actual
 * blocker, in the order the user can fix them. A disabled button must never
 * read "Send message".
 */
export function sendButtonState(s: SendButtonInput): { disabled: boolean; title: string } {
	if (!s.hasCredential) return { disabled: true, title: NO_CREDENTIAL_REASON };
	if (s.isUploading) return { disabled: true, title: 'Uploading files...' };
	if (s.isRecording) return { disabled: true, title: 'Recording...' };
	if (s.isTranscribing) return { disabled: true, title: 'Transcribing...' };
	if (!s.hasContent) return { disabled: true, title: 'Type a message to send' };
	return { disabled: false, title: s.autoBranch ? 'Branch will be auto-created from your message' : 'Send message' };
}
