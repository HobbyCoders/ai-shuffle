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

/** A fresh id for a run-settings request that is not an effort pick. */
export function newRunSettingsRequestId(): string {
	return newRequestId();
}

/**
 * Run-settings announcements in the order this tab received them, per
 * session. The worker announces a change before it answers the request, and
 * one socket delivers announcements in the order they were sent, so whatever
 * arrives after a request's own announcement was saved after that request.
 */
type AnnouncedField = 'effort' | 'context_window';
interface SessionAnnouncements {
	seq: number;
	/** request id → seq of the announcement that echoed it. */
	echoes: Map<string, number>;
	latest: Partial<Record<AnnouncedField, { seq: number; value: string | number | null }>>;
}
const announcements = new Map<string, SessionAnnouncements>();

/** Note a received `session_updated` announcement of run settings. */
export function noteRunSettingsAnnouncement(
	sessionId: string, updates: Partial<Record<AnnouncedField, string | number | null>>, requestId: unknown,
) {
	const fields = (['effort', 'context_window'] as const).filter(f => f in updates);
	if (!fields.length) return;
	let entry = announcements.get(sessionId);
	if (entry) announcements.delete(sessionId);
	else entry = { seq: 0, echoes: new Map(), latest: {} };
	announcements.set(sessionId, entry);
	while (announcements.size > SESSIONS) announcements.delete(announcements.keys().next().value as string);
	entry.seq += 1;
	if (typeof requestId === 'string' && requestId) {
		entry.echoes.set(requestId, entry.seq);
		while (entry.echoes.size > PER_SESSION) entry.echoes.delete(entry.echoes.keys().next().value as string);
	}
	for (const field of fields) entry.latest[field] = { seq: entry.seq, value: updates[field] ?? null };
}

/**
 * The newest value of `field` the worker announced after the announcement of
 * request `requestId`: a change saved after that request, by any tab. Nothing
 * when the request's own announcement has not arrived (anything seen so far
 * was saved before it) or nothing came after it.
 */
export function announcedAfter(sessionId: string, field: AnnouncedField, requestId: string | null): { value: string | number | null } | undefined {
	const entry = requestId ? announcements.get(sessionId) : undefined;
	const echoed = requestId ? entry?.echoes.get(requestId) : undefined;
	const latest = entry?.latest[field];
	if (echoed === undefined || !latest || latest.seq <= echoed) return undefined;
	return { value: latest.value };
}

/** Test hook. */
export function resetEffortEchoes() {
	sessions.clear();
	announcements.clear();
}
