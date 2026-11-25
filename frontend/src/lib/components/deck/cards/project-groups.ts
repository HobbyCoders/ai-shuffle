/** Copy and counts for deleting a project group (General tab). */

/** How many of these projects are in `group`; assignments of projects no longer listed don't count. */
export function groupMemberCount(assignments: Record<string, string> | undefined, group: string, projectIds: Iterable<string>): number {
	const listed = new Set(projectIds);
	return Object.entries(assignments ?? {}).filter(([id, name]) => name === group && listed.has(id)).length;
}

/** "Delete the group "Work"? 3 projects become ungrouped. No project is removed." */
export function groupDeleteMessage(group: string, members: number): string {
	const ungrouped = members === 1 ? '1 project becomes ungrouped' : `${members} projects become ungrouped`;
	return `Delete the group "${group}"? ${ungrouped}. No project is removed.`;
}
