/**
 * Composer attach/drop gate for remote-only compose.
 *
 * D8 uploads are project-dir-relative. A `projects_v2` with no local
 * `projects` row has nowhere to put files — disable attach/drop with
 * the same sentence the backend 400s.
 */

export const ATTACHMENTS_NEED_LOCAL_CHECKOUT =
	'Attachments need a local checkout of this project. Clone it here, or send without files.';

export const SELECT_PROJECT_TO_UPLOAD = 'Please select a project first to upload files.';

/**
 * Why attach/drop is blocked, or null when uploads may proceed.
 * Remote-only (`projectV2Id` set, no legacy `project`) is the new case;
 * no project at all keeps today's "select a project first" copy.
 */
export function attachmentBlockReason(
	project: string | null | undefined,
	projectV2Id: string | null | undefined
): string | null {
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
