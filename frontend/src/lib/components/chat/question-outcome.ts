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

/** Mirrors `SKIP_ANSWER_TEXT` (app/core/user_question_handler.py). */
export const QUESTION_SKIP_ANSWER_TEXT = 'User skipped this question.';

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
	if (toolName !== 'AskUserQuestion') return false;
	if (status !== 'error') return false;
	if (!result) return false;
	return result.includes(QUESTION_DISMISS_MESSAGE);
}
