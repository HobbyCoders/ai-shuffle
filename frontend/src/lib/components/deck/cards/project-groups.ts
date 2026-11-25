/** Copy and counts for deleting a project group (General tab). */

/**
 * How many assignments deleting `group` clears (``groups.deleteGroup``). The
 * assignments are a synced preference, so this counts projects listed only on
 * another worker too: the number matches what the delete ungroups.
 */
export function groupMemberCount(assignments: Record<string, string> | undefined, group: string): number {
	return Object.values(assignments ?? {}).filter((name) => name === group).length;
}

/** "Delete the group "Work"? 3 projects become ungrouped. No project is removed." */
export function groupDeleteMessage(group: string, members: number): string {
	const ungrouped = members === 1 ? '1 project becomes ungrouped' : `${members} projects become ungrouped`;
	return `Delete the group "${group}"? ${ungrouped}. No project is removed.`;
}
