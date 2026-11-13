/**
 * Which sessions are streaming across all the user's devices: the socket's
 * snapshot on every connect, then its live changes. Before the first snapshot,
 * the session list seeds it.
 */
import type { Session } from '$lib/api/client';
import { createStore } from './store-utils';

/** Cross-device streaming info for sidebar indicators */
export interface StreamingSessionInfo {
	projectId: string | null;
	worktreeId: string | null;
	/** Logical checkout branch. Another session on this SAME branch must
	 *  share the Git safety lock, even with different/absent legacy IDs. */
	worktreeBranch?: string | null;
}

const _ssStore = createStore<Map<string, StreamingSessionInfo>>(new Map());

// The socket's whole-state snapshot has arrived, so the map is live from here on.
let _ssSnapshotSeen = false;
// Sessions a live change named before that snapshot. The socket gets broadcasts
// before it sends its snapshot, so such a change can be newer than a session
// list still in flight, and the list must not undo it.
const _ssLiveBeforeSnapshot = new Set<string>();

/** Track which sessions are streaming across all user devices */
export const streamingSessions = {
	get current() { return _ssStore.current; },
	subscribe: _ssStore.subscribe,
	/** Whole state: the socket's snapshot. */
	set: (map: Map<string, StreamingSessionInfo>) => {
		_ssSnapshotSeen = true;
		_ssLiveBeforeSnapshot.clear();
		_ssStore.set(map);
	},
	/** A live change. Name the session it is about so a slower list cannot undo it. */
	update: (
		fn: (map: Map<string, StreamingSessionInfo>) => Map<string, StreamingSessionInfo>,
		sessionId?: string
	) => {
		if (sessionId && !_ssSnapshotSeen) _ssLiveBeforeSnapshot.add(sessionId);
		_ssStore.update(fn);
	},
};

type ListedStreamingRow = Pick<Session, 'id' | 'project_id' | 'is_streaming'> &
	Partial<Pick<Session, 'worktree_id' | 'worktree_branch' | 'branch'>>;

/**
 * A page that just loaded learns which chats run from the session list, until
 * the socket's snapshot replaces the map: before it, a running chat would read
 * as Done (#1527). Rows that do not say (search results, older servers), and
 * sessions a live change already reported, are left alone.
 */
export function seedStreamingFromList(rows: readonly ListedStreamingRow[]): void {
	if (_ssSnapshotSeen) return;
	const map = _ssStore.current;
	let next: Map<string, StreamingSessionInfo> | null = null;
	for (const row of rows) {
		if (typeof row.is_streaming !== 'boolean' || row.is_streaming === map.has(row.id)) continue;
		if (_ssLiveBeforeSnapshot.has(row.id)) continue;
		next ??= new Map(map);
		if (row.is_streaming) {
			next.set(row.id, {
				projectId: row.project_id ?? null,
				worktreeId: row.worktree_id ?? null,
				worktreeBranch: row.worktree_branch || row.branch || null
			});
		} else {
			next.delete(row.id);
		}
	}
	if (next) _ssStore.set(next);
}
