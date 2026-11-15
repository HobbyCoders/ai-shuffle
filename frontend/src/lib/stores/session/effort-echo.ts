/**
 * This tab's own effort picks, so their `session_updated` announcements are
 * not applied twice (#1633).
 *
 * Every acknowledged run-settings effort request is announced once to the
 * sockets of the worker the tab talks to, the picking tab's own included. The
 * tab already applies its own pick from the response, behind a generation
 * check that drops an outdated one. The announcement has no such check: one
 * that lands after a newer pick (for instance a level held for a model that
 * has not run yet) would put the older level back. So each request is noted
 * here and its one announcement is consumed instead of applied. Picks from
 * other tabs and devices name values this tab did not ask for and still apply.
 */

const SETTLED_TTL_MS = 15_000;

interface Echo {
	values: Array<string | null>;
	settledAt: number | null;
}

const echoes = new Map<string, Echo[]>();

function prune(sessionId: string, now = Date.now()) {
	const list = (echoes.get(sessionId) ?? []).filter(
		(e) => e.settledAt === null || now - e.settledAt < SETTLED_TTL_MS
	);
	if (list.length) echoes.set(sessionId, list);
	else echoes.delete(sessionId);
	return list;
}

/** Note an effort request about to be sent; returns its handle. */
export function noteEffortRequest(sessionId: string, value: string | null): Echo {
	const echo: Echo = { values: [value], settledAt: null };
	echoes.set(sessionId, [...prune(sessionId), echo]);
	return echo;
}

/** The request was acknowledged with `saved` (the level the server kept). */
export function settleEffortRequest(sessionId: string, echo: Echo, saved: string | null) {
	if (!echo.values.includes(saved)) echo.values.push(saved);
	echo.settledAt = Date.now();
	prune(sessionId);
}

/** The request failed: no announcement will come for it. */
export function dropEffortRequest(sessionId: string, echo: Echo) {
	const list = (echoes.get(sessionId) ?? []).filter((e) => e !== echo);
	if (list.length) echoes.set(sessionId, list);
	else echoes.delete(sessionId);
}

/** Whether an announced effort is this tab's own earlier pick; consumes it. */
export function consumeOwnEffortEcho(sessionId: string, value: string | null): boolean {
	const list = prune(sessionId);
	const index = list.findIndex((e) => e.values.includes(value));
	if (index < 0) return false;
	list.splice(index, 1);
	if (list.length) echoes.set(sessionId, list);
	else echoes.delete(sessionId);
	return true;
}

/** Test hook. */
export function resetEffortEchoes() {
	echoes.clear();
}
