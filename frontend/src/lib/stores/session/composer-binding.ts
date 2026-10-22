/**
 * Composer binding — the agent, tool access and Fast tier an EXISTING session
 * runs on, and how the composer tells that binding apart from a placeholder
 * (#418).
 *
 * A hidden Task worker has no sidebar row. Opening its transcript used to keep
 * the previous chat's agent and fabricate Ask and Fast off, and an owner
 * message typed there then sent those values back to the worker. The rule is
 * now stated once:
 *
 *   - A field is HYDRATED when its value came from the server (a session row,
 *     `/live`, an acknowledged pick) or from the user. Only those are shown as
 *     truth and only those travel on a send.
 *   - A field is UNHYDRATED while it holds a placeholder. The chip shows that
 *     it is loading and a send omits it, so the worker keeps its own binding
 *     (the backend falls back to the session row for an omitted key).
 *   - A PICK is an unsent local edit (a new agent, or the Fast value a model
 *     switch implied). `/live` never overwrites one; the next send carries it.
 *
 * Cached copies (the switcher's ephemeral cache, the tab's persisted session)
 * record which fields they hold as real values. A copy without that record
 * predates this rule and may hold the fabricated defaults, so it is not
 * trusted for these fields.
 */

export type ComposerBindingField = 'profile' | 'permission' | 'fast';
export type ComposerBindingFlags = Partial<Record<ComposerBindingField, true>>;

export const COMPOSER_BINDING_FIELDS: readonly ComposerBindingField[] = ['profile', 'permission', 'fast'];

/** All three fields: a session opened with nothing known about its binding. */
export const ALL_BINDING_UNHYDRATED: ComposerBindingFlags = Object.freeze({ profile: true, permission: true, fast: true });

export interface ComposerBindingValues {
	profile: string;
	permissionModeOverride: string | null;
	fastMode: boolean;
}

export interface ComposerBindingState extends ComposerBindingValues {
	bindingUnhydrated?: ComposerBindingFlags;
	bindingPicks?: ComposerBindingFlags;
}

/** What a cache holds for these fields, and which of them it holds as real values. */
export interface CachedComposerBinding {
	profile?: string;
	permissionModeOverride?: string | null;
	fastMode?: boolean;
	bindingHydrated?: ComposerBindingFlags;
	bindingPicks?: ComposerBindingFlags;
}

/** The binding fields of a session-list row. */
export interface ComposerBindingRow {
	profile_id?: string | null;
	permission_mode?: string | null;
	fast?: boolean | null;
}

export function isBindingUnhydrated(state: Pick<ComposerBindingState, 'bindingUnhydrated'>, field: ComposerBindingField): boolean {
	return state.bindingUnhydrated?.[field] === true;
}

function withFlag(flags: ComposerBindingFlags | undefined, field: ComposerBindingField, on: boolean): ComposerBindingFlags {
	const next: ComposerBindingFlags = { ...(flags ?? {}) };
	if (on) next[field] = true;
	else delete next[field];
	return next;
}

/** State patch: `field` now holds a real value (server truth or an acknowledged pick). */
export function markBindingHydrated(state: ComposerBindingState, field: ComposerBindingField): Pick<ComposerBindingState, 'bindingUnhydrated'> {
	return { bindingUnhydrated: withFlag(state.bindingUnhydrated, field, false) };
}

/** State patch: `field` was changed locally and has not been sent yet. */
export function markBindingPicked(state: ComposerBindingState & { sessionId: string | null }, field: ComposerBindingField): Pick<ComposerBindingState, 'bindingUnhydrated' | 'bindingPicks'> {
	return {
		bindingUnhydrated: withFlag(state.bindingUnhydrated, field, false),
		// A new compose has no server binding to protect a pick from.
		bindingPicks: state.sessionId ? withFlag(state.bindingPicks, field, true) : (state.bindingPicks ?? {}),
	};
}

/** The cache record for these fields: values plus which of them are real. */
export function cacheComposerBinding(state: ComposerBindingState): CachedComposerBinding {
	const hydrated: ComposerBindingFlags = {};
	for (const field of COMPOSER_BINDING_FIELDS) {
		if (!isBindingUnhydrated(state, field)) hydrated[field] = true;
	}
	return {
		profile: state.profile,
		permissionModeOverride: state.permissionModeOverride,
		fastMode: state.fastMode,
		bindingHydrated: hydrated,
		bindingPicks: { ...(state.bindingPicks ?? {}) },
	};
}

/** A cache field is usable only when the cache says it holds a real value. */
function trusted(cache: CachedComposerBinding | null | undefined, field: ComposerBindingField): boolean {
	return !!cache && (cache.bindingPicks?.[field] === true || cache.bindingHydrated?.[field] === true);
}

/**
 * The binding a switch-in shows for an existing session:
 * cached pick or real cached value > the session-list row > a placeholder
 * marked unhydrated for `/live` to fill. Never the leaving session's values.
 *
 * `rowPermissionWins` is the #371 rule: a cache saved before a project was
 * attached holds an access the attach reset, so the row decides.
 */
export function restoreComposerBinding(
	cache: CachedComposerBinding | null | undefined,
	row: ComposerBindingRow | null | undefined,
	options: { rowPermissionWins?: boolean } = {},
): Required<ComposerBindingState> {
	const unhydrated: ComposerBindingFlags = {};
	const picks: ComposerBindingFlags = {};

	let profile = '';
	if (trusted(cache, 'profile') && cache!.profile) {
		profile = cache!.profile;
		if (cache!.bindingPicks?.profile) picks.profile = true;
	} else if (row?.profile_id) {
		profile = row.profile_id;
	} else if (row && row.profile_id === null) {
		// The session's agent was deleted (ON DELETE SET NULL): known to have
		// none, so the chip asks for one instead of loading forever.
	} else {
		unhydrated.profile = true;
	}

	let permissionModeOverride: string | null = null;
	if (options.rowPermissionWins && row) {
		permissionModeOverride = row.permission_mode ?? 'default';
	} else if (trusted(cache, 'permission') && cache!.permissionModeOverride != null) {
		permissionModeOverride = cache!.permissionModeOverride;
	} else if (row) {
		permissionModeOverride = row.permission_mode ?? null;
	} else if (!trusted(cache, 'permission')) {
		unhydrated.permission = true;
	}

	let fastMode = false;
	if (trusted(cache, 'fast') && typeof cache!.fastMode === 'boolean') {
		fastMode = cache!.fastMode;
		if (cache!.bindingPicks?.fast) picks.fast = true;
	} else if (row && typeof row.fast === 'boolean') {
		fastMode = row.fast;
	} else {
		unhydrated.fast = true;
	}

	return { profile, permissionModeOverride, fastMode, bindingUnhydrated: unhydrated, bindingPicks: picks };
}

/** The `/live` binding fields; `undefined` means an older worker that does not send them. */
export interface LiveComposerBinding {
	profile_id?: string | null;
	permission_mode?: string | null;
	fast?: boolean | null;
}

/**
 * The part of a `/live` answer the composer may apply. Server truth replaces
 * everything except an unsent pick and a value changed while the request was
 * in flight (`initial` is the state when it was sent). A field the server did
 * not report stays as it is, unhydrated if it was.
 */
export function liveComposerBindingPatch(
	meta: LiveComposerBinding,
	current: ComposerBindingState,
	initial: ComposerBindingValues,
	normalizePermission: (mode: string | null | undefined) => string | null,
): Partial<ComposerBindingState> {
	const patch: Partial<ComposerBindingState> = {};
	let unhydrated = current.bindingUnhydrated ?? {};
	const picks = current.bindingPicks ?? {};

	// `null` is real too: the session's agent was deleted (ON DELETE SET NULL),
	// so the composer shows "pick an agent" rather than loading forever.
	if (meta.profile_id !== undefined && !picks.profile && current.profile === initial.profile) {
		patch.profile = meta.profile_id ?? '';
		unhydrated = withFlag(unhydrated, 'profile', false);
	}
	if (meta.permission_mode !== undefined && !picks.permission && current.permissionModeOverride === initial.permissionModeOverride) {
		patch.permissionModeOverride = normalizePermission(meta.permission_mode);
		unhydrated = withFlag(unhydrated, 'permission', false);
	}
	if (typeof meta.fast === 'boolean' && !picks.fast && current.fastMode === initial.fastMode) {
		patch.fastMode = meta.fast;
		unhydrated = withFlag(unhydrated, 'fast', false);
	}
	if (unhydrated !== (current.bindingUnhydrated ?? {})) patch.bindingUnhydrated = unhydrated;
	return patch;
}

/**
 * What a send may carry for these fields. An unhydrated field is left out so
 * the backend keeps the session's own value; it never sends a placeholder.
 */
export function sendableComposerBinding(state: ComposerBindingState & { sessionId: string | null }): {
	profile: string | undefined;
	permissionMode: string | undefined;
	fast: boolean | undefined;
} {
	const existing = !!state.sessionId;
	return {
		// An existing session resolves an omitted agent to its own (chat_ws_handler).
		profile: existing && (isBindingUnhydrated(state, 'profile') || !state.profile) ? undefined : state.profile,
		permissionMode: existing && isBindingUnhydrated(state, 'permission') ? undefined : (state.permissionModeOverride || undefined),
		fast: existing && isBindingUnhydrated(state, 'fast') ? undefined : state.fastMode,
	};
}
