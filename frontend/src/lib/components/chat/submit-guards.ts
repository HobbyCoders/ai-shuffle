/**
 * Precondition guards for sending a chat message.
 *
 * These mirror the early-return checks that used to live inline in
 * `ChatInput.handleSubmit`. Pulling them out keeps a single, testable source of
 * truth for *why* a send is blocked — so the UI can surface the reason with a
 * toast instead of silently swallowing the click (which read as a dead app).
 *
 * The order matters and is preserved exactly from the original inline guards:
 *   empty composer → upload in flight → worktree being created → no profile → no project.
 */
import type { NotificationSeverity } from '$lib/stores/notifications';

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
	/** A project is selected. */
	hasProject: boolean;
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
		return { notice: { message: 'Select a profile first.', tone: 'warning' } };
	}
	if (!s.hasProject) {
		return { notice: { message: 'Select a project first.', tone: 'warning' } };
	}

	return null;
}
