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

/** A chat another worker runs whose project has no shared id: nothing here
 * names that worker's project, and its id can name an unrelated one here. */
export const ATTACHMENTS_REMOTE_UNLINKED =
	'Attachments aren’t available for this chat from this worker: another worker runs it and its project isn’t linked here. Attach the file from the worker that runs the chat, or send without files.';

export const SELECT_PROJECT_TO_UPLOAD = 'Please select a project first to upload files.';

/** The owner of the open chat stayed unknown (its live state did not answer). */
export const UPLOAD_OWNER_UNKNOWN =
	'couldn’t confirm which worker runs this chat yet. Try again in a moment';

/** Another chat was opened before the upload could start. */
export const UPLOAD_CHAT_CHANGED = 'the chat changed before the file was attached';

/** Where a composer upload goes, decided by {@link composerUploadTarget}. */
export type ComposerUploadTarget =
	| { kind: 'chat' }
	| { kind: 'session'; sessionId: string }
	| { kind: 'project'; project: string }
	| { kind: 'wait' }
	| { kind: 'blocked'; reason: string };

/**
 * The one place that decides where a composer upload goes (#1659), from who
 * runs the open chat, never from which ids the store happens to hold. A legacy
 * project id names a folder on this worker only for a chat this worker runs: on
 * another worker's chat it can name an unrelated project here.
 *   - owner unknown and a local id held: wait (the id may name an unrelated
 *     project on this worker until a row or `/live` says who runs the chat)
 *   - owner remote: the session upload (the server routes it to the owner),
 *     whatever local id the store still holds
 *   - owner local: the local project
 * A shared id with no local id uses the session upload whoever runs the chat.
 * A remote chat with no shared id (a legacy or not yet linked project) has no
 * session upload (the server needs the shared id) and its local id may name an
 * unrelated project here: attaching is refused with a way forward.
 * New chats (no session yet) upload to the project picked for them.
 */
export function composerUploadTarget(
	state: { sessionId: string | null; project: string | null | undefined; projectV2Id?: string | null; isRemote?: boolean },
	mode: SessionMode = 'code'
): ComposerUploadTarget {
	if (mode === 'chat') return { kind: 'chat' };
	const { sessionId, project, projectV2Id, isRemote } = state;
	if (!sessionId) {
		if (project) return { kind: 'project', project };
		return { kind: 'blocked', reason: projectV2Id ? ATTACHMENTS_NEED_LOCAL_CHECKOUT : SELECT_PROJECT_TO_UPLOAD };
	}
	if (projectV2Id && (isRemote === true || !project)) return { kind: 'session', sessionId };
	if (!project) return { kind: 'blocked', reason: SELECT_PROJECT_TO_UPLOAD };
	if (isRemote === true) return { kind: 'blocked', reason: ATTACHMENTS_REMOTE_UNLINKED };
	if (isRemote === undefined) return { kind: 'wait' };
	return { kind: 'project', project };
}

/**
 * Why attach/drop is blocked, or null when uploads may proceed: the blocked
 * outcome of {@link composerUploadTarget}, so the attach control says before a
 * file is picked what an upload would be told after.
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
	sessionId?: string | null,
	isRemote?: boolean
): string | null {
	const target = composerUploadTarget({ sessionId: sessionId ?? null, project, projectV2Id, isRemote }, mode);
	return target.kind === 'blocked' ? target.reason : null;
}

/** True when attach/drop should be inert (remote-only compose). */
export function isRemoteOnlyCompose(
	project: string | null | undefined,
	projectV2Id: string | null | undefined
): boolean {
	return !!projectV2Id && !project;
}
