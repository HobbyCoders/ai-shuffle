/**
 * Composer attach/drop gate, per lane.
 *
 * The code lane's uploads are project-dir-relative. A `projects_v2` with no
 * local `projects` row has nowhere to put files — disable attach/drop with the
 * same sentence the backend 400s.
 *
 * The chat lane has no project and never will: its uploads go to a shared
 * `chat-uploads` directory under the workspace root, and the chat tool ceiling
 * carries a `Read` scoped to exactly that directory. So "no project" is the
 * normal, working state there rather than something the user must go fix.
 */

import type { SessionMode } from '$lib/stores/sessionMode.svelte';

export const ATTACHMENTS_NEED_LOCAL_CHECKOUT =
	'Attachments need a local checkout of this project. Clone it here, or send without files.';

export const SELECT_PROJECT_TO_UPLOAD = 'Please select a project first to upload files.';

/**
 * Why attach/drop is blocked, or null when uploads may proceed.
 *
 * `mode` defaults to the code lane so callers that predate lanes keep their
 * behaviour: remote-only (`projectV2Id` set, no legacy `project`) blocks with
 * the API's own sentence, and no project at all keeps the "select a project
 * first" copy. In the chat lane neither applies — nothing is ever blocked.
 */
export function attachmentBlockReason(
	project: string | null | undefined,
	projectV2Id: string | null | undefined,
	mode: SessionMode = 'code'
): string | null {
	if (mode === 'chat') return null;
	if (projectV2Id && !project) return ATTACHMENTS_NEED_LOCAL_CHECKOUT;
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
