/**
 * AskUserQuestion outcomes that are NOT failures.
 *
 * Closing the question modal ("X") has no "no answer" value the AskUserQuestion
 * tool accepts, so the backend resolves the pending call by DENYING it with a
 * dismissal message (see `DISMISS_MESSAGE` in app/core/user_question_handler.py).
 * The deny is the correct mechanic — but a denied tool call renders with the
 * same red error chrome as a real permission denial, which reads as "something
 * went wrong" for what was a deliberate, benign choice.
 *
 * The deny message is the only marker that survives to the frontend: every
 * ingest path (live v2 events, the v1 socket path, DB reload, JSONL backfill)
 * carries the tool_result TEXT, and none of them carries a structured
 * denial-kind field. So we recognize the outcome by its exact text, gated on
 * the tool name — deliberately conservative, since a miss must degrade to
 * today's error rendering rather than mislabel a real denial.
 */

/** Mirrors `DISMISS_MESSAGE` (app/core/user_question_handler.py). */
export const QUESTION_DISMISS_MESSAGE =
	'User dismissed the question dialog without answering.';

/** Card label for a dismissed question. */
export const QUESTION_DISMISSED_LABEL = 'Dismissed — user closed the question dialog';

/**
 * True when this tool card is an AskUserQuestion call the user dismissed.
 *
 * Requires all three: the AskUserQuestion tool, an error/denied outcome, and
 * the backend's exact dismissal text. Any other denied AskUserQuestion (a
 * profile-disabled tool, a real permission rule) keeps the error rendering.
 */
export function isDismissedQuestion(
	toolName: string | undefined,
	status: string | undefined,
	result: string | undefined
): boolean {
	if (toolName !== 'AskUserQuestion' && toolName !== 'AskUser') return false;
	if (status !== 'error') return false;
	if (!result) return false;
	return result.includes(QUESTION_DISMISS_MESSAGE);
}

/**
 * Mirrors the "queued" reply the delegated Claude engine gives the CLI's
 * AskUserQuestion call (fluid/fluid/engine/delegated.py). The question card is
 * posted and the answer arrives later as a message, so the engine has to
 * DENY the CLI's own tool to stop it waiting for an answer. The CLI then
 * marks the result as an error, even though posting the question worked.
 */
export const QUESTION_POSTED_MESSAGE = 'Questions posted.';

/**
 * Mirrors the start of `AWAY_NOTE` (app/core/away_mode.py). When the owner is
 * away the host answers at once, and the delegated engine sends that answer
 * back through the same deny, so it too arrives flagged as an error.
 */
export const QUESTION_AWAY_ANSWER_PREFIX = 'Owner is away — recommended default applied';

/**
 * True when this tool card is an AskUserQuestion call the host handled (the
 * questions were posted, or answered for an away owner), even though the
 * result arrived flagged as an error.
 *
 * Requires the question tool, the error outcome and one of the host's own
 * reply texts, so any other denial keeps the error rendering.
 */
export function isPostedQuestion(
	toolName: string | undefined,
	status: string | undefined,
	result: string | undefined
): boolean {
	if (toolName !== 'AskUserQuestion' && toolName !== 'AskUser') return false;
	if (status !== 'error') return false;
	if (!result) return false;
	if (result.includes(QUESTION_AWAY_ANSWER_PREFIX)) return true;
	return result.includes(QUESTION_POSTED_MESSAGE) && /"status":\s*"queued"/.test(result);
}
