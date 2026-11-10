import { api } from '$lib/api/client';
import type { ActiveSessionState } from '$lib/stores/active-session';

/** How long Send waits for the server to say where an open chat runs (#1364). */
export const OWNER_CHECK_MS = 2000;

/**
 * Asks this worker, now, whether the chat runs on another worker: true, false,
 * or null when there is no answer (an error, no answer within `limitMs`, or a
 * worker too old to report `is_remote`).
 *
 * A fresh request every time, never a shared or cached one: a request already
 * in flight may have started before a hand-off.
 */
export async function askRunsElsewhere(sessionId: string, limitMs = OWNER_CHECK_MS): Promise<boolean | null> {
	const abort = new AbortController();
	let timer: ReturnType<typeof setTimeout> | undefined;
	const limit = new Promise<null>((resolve) => {
		timer = setTimeout(() => { abort.abort(); resolve(null); }, limitMs);
	});
	const answer = api.get<{ session_meta?: { is_remote?: unknown } }>(
		`/sessions/${encodeURIComponent(sessionId)}/live?include_messages=false&owner_fields=true`,
		{ signal: abort.signal, cache: 'no-store' }
	).then((live) => {
		const isRemote = live?.session_meta?.is_remote;
		return typeof isRemote === 'boolean' ? isRemote : null;
	}, () => null);
	try {
		return await Promise.race([answer, limit]);
	} finally {
		clearTimeout(timer);
	}
}

/**
 * Whether a note chip in a message sent now goes as `@Notes/<id>` rather than
 * this worker's file path (#1364). A reference resolves on any worker; the path
 * is right only for a chat this worker runs. Call it only for a message that
 * has a note chip with an id: an open chat costs one request.
 *
 * - A new chat: the "Runs on" pick (`deviceIntent`; `null` is this device).
 * - A message held for a queued move: the move sends it to the new worker.
 * - An open chat: the server's answer, asked at send time. Only "runs here"
 *   sends the path; any other outcome sends the reference.
 *
 * Returns a boolean when no request is needed, else a promise.
 */
export function notesGoAsReferences(
	state: Pick<ActiveSessionState, 'sessionId' | 'deviceIntent'>,
	moving: boolean,
	ask: (sessionId: string) => Promise<boolean | null> = askRunsElsewhere
): boolean | Promise<boolean> {
	if (!state.sessionId) return !!state.deviceIntent?.id;
	if (moving) return true;
	return ask(state.sessionId).then((elsewhere) => elsewhere !== false);
}
