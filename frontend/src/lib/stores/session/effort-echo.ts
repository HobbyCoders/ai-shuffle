/**
 * This tab's own effort requests, so an outdated announcement of one cannot
 * undo a newer pick (#1633).
 *
 * Every run-settings effort request carries a fresh `request_id`, and the
 * worker that took it echoes that id on its `session_updated` announcement.
 * The tab already applies its own pick from the response, behind a generation
 * check. The announcement has none, so one that lands after a newer pick from
 * this tab (a level held for a model that has not run yet, a quick second
 * pick) would put the older level back. Such an announcement is skipped.
 *
 * Only the id identifies a request: a value never does, so another tab's pick
 * of the same level is always applied, and an announcement that never arrives
 * (a dropped socket) cannot swallow a later one. Ids are kept per session, the
 * newest few only, so nothing grows without bound. An announcement without an
 * id (an older worker) is treated as another tab's pick.
 */

/** Requests remembered per session; older ones are forgotten first. */
const PER_SESSION = 32;
/** Sessions remembered; the least recently used is forgotten first. */
const SESSIONS = 64;

interface SessionRequests {
	/** request id → order it was sent in (1, 2, …). */
	ids: Map<string, number>;
	latest: number;
}

const sessions = new Map<string, SessionRequests>();

function newRequestId(): string {
	const cryptoApi = globalThis.crypto as Crypto | undefined;
	if (cryptoApi?.randomUUID) return cryptoApi.randomUUID();
	return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Note an effort request about to be sent; returns the id to send with it. */
export function noteEffortRequest(sessionId: string): string {
	let entry = sessions.get(sessionId);
	if (entry) sessions.delete(sessionId);
	else entry = { ids: new Map(), latest: 0 };
	sessions.set(sessionId, entry); // most recently used last
	while (sessions.size > SESSIONS) sessions.delete(sessions.keys().next().value as string);
	const id = newRequestId();
	entry.latest += 1;
	entry.ids.set(id, entry.latest);
	while (entry.ids.size > PER_SESSION) entry.ids.delete(entry.ids.keys().next().value as string);
	return id;
}

/**
 * Whether an announced effort answers one of this tab's own requests that a
 * newer request from this tab has since replaced. Such an announcement must
 * not be applied. This tab's newest request, another tab's request and an
 * announcement without an id all return false and are applied as usual.
 */
export function isOutdatedOwnEffortEcho(sessionId: string, requestId: unknown): boolean {
	if (typeof requestId !== 'string' || !requestId) return false;
	const entry = sessions.get(sessionId);
	const order = entry?.ids.get(requestId);
	if (!entry || order === undefined) return false;
	return order < entry.latest;
}

/** Test hook. */
export function resetEffortEchoes() {
	sessions.clear();
}
