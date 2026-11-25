/**
 * Copy for "Remove project and chats" (the project's danger row and the
 * Workers tab). The numbers come from the worker that removes: the chats it
 * deletes and the worktree folders whose records go while the folders stay.
 */

/** `null`: the worker did not say (an older worker); the copy names no number. */
export interface RemovalCounts {
	chats: number | null;
	worktrees: number | null;
}

function plural(n: number, one: string, many: string): string {
	return `${n} ${n === 1 ? one : many}`;
}

/** "Remove Demo from Worker A and its 3 chats? This cannot be undone. …" */
export function removalConfirmText(name: string, where: string | null, counts: RemovalCounts): string {
	const target = `${name}${where ? ` from ${where}` : ''}`;
	const chats = counts.chats;
	const head = chats === null
		? `Remove ${target} and all its chats?`
		: chats === 0
			? `Remove ${target}? It has no chats.`
			: `Remove ${target} and its ${plural(chats, 'chat', 'chats')}?`;
	const parts = [head, chats === 0 ? 'Files stay on disk.' : 'This cannot be undone. Files stay on disk.'];
	if (counts.worktrees) {
		parts.push(`${plural(counts.worktrees, 'worktree folder stays', 'worktree folders stay')} on disk too.`);
	}
	return parts.join(' ');
}
