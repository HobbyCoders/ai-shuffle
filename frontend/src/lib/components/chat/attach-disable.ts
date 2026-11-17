/**
 * Composer attach/drop gate, per lane.
 *
 * Local uploads are project-dir-relative. Existing canonical worker sessions
 * can instead stage files in a session-scoped directory for upload-blob delivery.
 * A remote-only draft has no session-scoped destination yet and stays blocked.
 *
 * The chat lane has no project and never will: its uploads go to a shared
 * `chat-uploads` directory under the workspace root, and the chat tool ceiling
 * carries a `Read` scoped to exactly that directory. So "no project" is the
 * normal, working state there rather than something the user must go fix.
 */

import type { SessionMode } from '$lib/stores/sessionMode.svelte';

export const ATTACHMENTS_NEED_LOCAL_CHECKOUT =
	'Attachments need a local checkout of this project. Clone it here, or send without files.';

/** A new chat for a project this worker has no checkout of, with files the
 * owner attached before picking it: only the worker that has the project can
 * take them, so the send waits for that pick (the draft and its chips stay). */
export const ATTACHMENTS_NEED_RUNS_ON =
	'This project isn’t checked out here, so its attachments need the worker that has it. Pick that worker in Runs on, or send without files.';

export const SELECT_PROJECT_TO_UPLOAD = 'Please select a project first to upload files.';

/** The owner of the open chat stayed unknown (its live state did not answer). */
export const UPLOAD_OWNER_UNKNOWN =
	'couldn’t confirm which worker runs this chat yet. Try again in a moment';

/**
 * Whether an upload must wait until the page knows who runs the open chat
 * (#1659). A legacy project id names a folder on this worker only for a chat
 * this worker runs: on another worker's chat it can name an unrelated project
 * here. Until a row or `/live` says who runs it (a reload, a deep link, a
 * restored tab, `/live` slow or failing), the project upload would write into
 * whatever local project carries that id. A session upload needs no wait: the
 * server routes it by the session's own owner. New chats have no owner yet and
 * upload to the project picked for them.
 */
export function uploadNeedsOwner(
	state: { sessionId: string | null; project: string | null | undefined; isRemote?: boolean },
	mode: SessionMode = 'code'
): boolean {
	return mode !== 'chat' && !!state.sessionId && !!state.project && state.isRemote === undefined;
}

/**
 * Why attach/drop is blocked, or null when uploads may proceed.
 *
 * `mode` defaults to the code lane so callers that predate lanes keep their
 * behaviour: remote-only drafts (`projectV2Id` set, no legacy `project` or
 * `sessionId`) block with the API's own sentence, and no project at all keeps
 * the "select a project first" copy. In the chat lane neither applies — nothing is ever blocked.
 */
export function attachmentBlockReason(
	project: string | null | undefined,
	projectV2Id: string | null | undefined,
	mode: SessionMode = 'code',
	sessionId?: string | null
): string | null {
	if (mode === 'chat') return null;
	if (projectV2Id && !project) return sessionId ? null : ATTACHMENTS_NEED_LOCAL_CHECKOUT;
	if (!project) return SELECT_PROJECT_TO_UPLOAD;
	return null;
}

/** True when attach/drop should be inert (remote-only compose). */
export function isRemoteOnlyCompose(
	project: string | null | undefined,
	projectV2Id: string | null | undefined
): boolean {
	return !!projectV2Id && !project;
}
