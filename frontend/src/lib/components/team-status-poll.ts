/**
 * Polling policy for the agent-teams status bar.
 *
 * Before (PERFORMANCE_REVIEW §3.6 F4): `GET /api/v1/teams/{sessionId}` every 2 s
 * for EVERY open session, forever — an auth check plus a DB session-access check
 * per hit — even for the overwhelming majority of sessions that have never had a
 * team, and with no document-visibility gate. That is the single largest idle
 * frontend request source after git polling.
 *
 * After: the SSE feed already materialises `team.member_*` events as
 * `team_member` transcript cards, so the transcript itself says whether this
 * session has teams and whether any teammate is running. That signal decides the
 * cadence; polling only fills in what the cards do not carry (pid, token usage)
 * and only while there is something to fill in.
 */

export interface TeamPollInput {
	/** Document visibility — a hidden tab polls nothing. */
	visible: boolean;
	/** Any team is known to exist for this session (from the feed or a poll). */
	hasTeams: boolean;
	/** Any teammate is currently running. */
	hasRunning: boolean;
}

/** Fast cadence while a teammate is actually running. */
export const TEAM_POLL_RUNNING_MS = 2000;
/** Teams exist but nothing is running: refresh rarely, just to catch restarts. */
export const TEAM_POLL_IDLE_MS = 30000;

/**
 * Interval for the next poll, or `null` for "do not poll".
 *
 * `null` is the common case: no teams have ever been seen, or the tab is hidden.
 * The feed wakes the bar when that changes, so nothing is missed by not asking.
 */
export function resolveTeamPollIntervalMs(input: TeamPollInput): number | null {
	if (!input.visible) return null;
	if (!input.hasTeams) return null;
	return input.hasRunning ? TEAM_POLL_RUNNING_MS : TEAM_POLL_IDLE_MS;
}

/** The transcript-derived team signal (cheap single pass over the messages). */
export interface TeamFeedSignal {
	hasTeams: boolean;
	hasRunning: boolean;
	/** Changes whenever the derived team state changes — a refresh trigger. */
	signature: string;
}

export interface TeamMemberLike {
	type?: string;
	teamName?: string;
	teammateName?: string;
	teammateStatus?: string;
}

/**
 * Derive team presence/activity from transcript cards.
 *
 * O(N) with a type check per message, and only over the messages already in
 * memory — no request, no server work. The signature folds every teammate's
 * status so the component can refetch exactly when the feed reports a change
 * (a team appearing, a teammate finishing) instead of on a timer.
 */
export function deriveTeamFeedSignal(messages: readonly TeamMemberLike[]): TeamFeedSignal {
	let hasTeams = false;
	let hasRunning = false;
	const parts: string[] = [];
	for (const message of messages) {
		if (message.type !== 'team_member') continue;
		hasTeams = true;
		const status = message.teammateStatus ?? '';
		if (status === 'running') hasRunning = true;
		parts.push(`${message.teamName ?? ''}::${message.teammateName ?? ''}:${status}`);
	}
	return { hasTeams, hasRunning, signature: parts.join('|') };
}
