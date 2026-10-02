/**
 * API users: the data and actions behind Integrations > API users.
 *
 * Rules carried over from the old ApiUserManager:
 * - Create sends `name` trimmed, an omitted (undefined) description, project
 *   and agent list when empty, and `null` model/effort for "Agent default".
 * - Updates send `null` (never undefined) to clear the description or the
 *   project restriction; PATCH ignores omitted fields.
 * - The API key is shown once, after Create or Rotate, then only dismissed.
 * - Picking a model reconciles the effort with what that model supports.
 * - Connector sessions always ask; no permission setting exists.
 *
 * "Add API user" makes one local draft (never two). The draft has no id on
 * the worker until Create succeeds.
 */
import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
import { api, type Profile } from '$lib/api/client';
import {
	createApiUser,
	deleteApiUser,
	getApiUsers,
	rotateApiUserKey,
	updateApiUser,
	type ApiUser,
	type ApiUserUpdate
} from '$lib/api/apiUsers';
import type { Session } from '$lib/api/sessions';
import { isForbidden } from '$lib/settings/access.svelte';
import { EFFORT_OPTIONS, modelEffortOptions, reconcileModelEffort, resolveModelEffort } from '$lib/constants/model-effort';

export interface ApiProject {
	id: string;
	name: string;
	description?: string;
	path: string;
}

/** Local fields of the one draft row. */
export interface ApiUserDraft {
	name: string;
	description: string;
	projectId: string;
	profileIds: string[];
	model: string;
	effort: string;
}

/** List and selection id of the draft. Real ids come from the worker. */
export const DRAFT_ID = 'new-api-user';
export const LOAD_ERROR = 'Failed to load data';
export const NAME_REQUIRED = 'Name is required';

export function emptyDraft(): ApiUserDraft {
	return { name: '', description: '', projectId: '', profileIds: [], model: '', effort: '' };
}

/** Effort choices for a model: "Agent default" first, then what the model supports. */
export function effortChoices(model: string, effort: string): { value: string; label: string }[] {
	const levels = model ? modelEffortOptions(model, effort || null) : EFFORT_OPTIONS;
	return [{ value: '', label: 'Agent default' }, ...levels.map((l) => ({ value: l.value, label: l.label }))];
}

/** The effort to keep when the model changes (unchanged when unknown). */
export function effortForModel(model: string, effort: string): string {
	if (!model) return effort;
	return reconcileModelEffort(effort || null, resolveModelEffort(model)) ?? '';
}

/** Session timestamps are offset-aware or naive UTC; naive ones get a Z. */
export function parseTimestamp(value: string): Date {
	const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value);
	return new Date(hasOffset ? value : value + 'Z');
}

/** "12m ago", "3h ago", "2d ago", or a date. Empty when unknown. */
export function relativeAgo(value: string | null | undefined, now = Date.now()): string {
	if (!value) return '';
	const date = parseTimestamp(value);
	if (isNaN(date.getTime())) return '';
	const mins = Math.floor((now - date.getTime()) / 60000);
	if (mins < 1) return 'just now';
	if (mins < 60) return `${mins}m ago`;
	const hours = Math.floor(mins / 60);
	if (hours < 24) return `${hours}h ago`;
	const days = Math.floor(hours / 24);
	if (days < 7) return `${days}d ago`;
	if (days < 30) return `${Math.floor(days / 7)}w ago`;
	return date.toLocaleDateString();
}

/** Short date for facts: "Jun 4", or "Never". */
export function shortDate(value: string | null | undefined): string {
	if (!value) return 'Never';
	const date = parseTimestamp(value);
	if (isNaN(date.getTime())) return 'Never';
	return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** Sessions of one API user, most recently updated first. */
export function sessionsFor(sessions: readonly Session[], userId: string): Session[] {
	return sessions
		.filter((s) => s.api_user_id === userId)
		.slice()
		.sort((a, b) => parseTimestamp(b.updated_at).getTime() - parseTimestamp(a.updated_at).getTime());
}

export function sessionLabel(session: Session): string {
	return session.title?.trim() || session.id.slice(0, 8);
}

export class ApiUsersModel {
	users = $state<ApiUser[]>([]);
	profiles = $state<Profile[]>([]);
	projects = $state<ApiProject[]>([]);
	loading = $state(true);
	forbidden = $state(false);
	/** Load or action failure, shown above the detail. */
	error = $state('');
	/** Draft row, or null. Only one at a time. */
	draft = $state<ApiUserDraft | null>(null);
	draftError = $state('');
	creating = $state(false);
	/** Field saves in flight, keyed by `${userId}:${field}`. */
	saving = $state<Record<string, boolean>>({});
	/** The one-time key, tied to the user it belongs to. */
	revealed = $state<{ userId: string; key: string } | null>(null);
	copied = $state(false);
	#copyTimer: ReturnType<typeof setTimeout> | undefined;

	userById(id: string | null | undefined): ApiUser | undefined {
		return id ? this.users.find((u) => u.id === id) : undefined;
	}

	projectName(id: string | null | undefined): string | null {
		if (!id) return null;
		return this.projects.find((p) => p.id === id)?.name || id;
	}

	profileName(id: string | null | undefined): string | null {
		if (!id) return null;
		return this.profiles.find((p) => p.id === id)?.name || id;
	}

	async load(): Promise<void> {
		try {
			const [users, profiles, projects] = await Promise.all([
				getApiUsers(),
				api.get<Profile[]>('/agents'),
				api.get<ApiProject[]>('/projects')
			]);
			this.users = users;
			this.profiles = profiles;
			this.projects = projects;
			this.forbidden = false;
			// A later good reload clears a stale load failure, not an action's error.
			if (this.error === LOAD_ERROR) this.error = '';
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.error = LOAD_ERROR;
		} finally {
			this.loading = false;
		}
	}

	/** Start (or return to) the one draft. Returns its selection id. */
	addDraft(): string {
		if (!this.draft) this.draft = emptyDraft();
		this.draftError = '';
		return DRAFT_ID;
	}

	discardDraft(): void {
		this.draft = null;
		this.draftError = '';
	}

	setDraftModel(model: string): void {
		if (!this.draft) return;
		this.draft.effort = effortForModel(model, this.draft.effort);
		this.draft.model = model;
	}

	toggleDraftProfile(id: string): void {
		if (!this.draft) return;
		const ids = this.draft.profileIds;
		this.draft.profileIds = ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id];
	}

	/** Create the draft. Returns the new user's id, or null when it failed. */
	async createDraft(): Promise<string | null> {
		const d = this.draft;
		if (!d || this.creating) return null;
		if (!d.name.trim()) {
			this.draftError = NAME_REQUIRED;
			return null;
		}
		this.creating = true;
		this.draftError = '';
		try {
			const result = await createApiUser({
				name: d.name.trim(),
				description: d.description.trim() || undefined,
				project_id: d.projectId || undefined,
				profile_ids: d.profileIds.length > 0 ? d.profileIds : undefined,
				model: d.model || null,
				effort: d.effort || null
			});
			this.revealed = { userId: result.id, key: result.api_key };
			this.copied = false;
			this.draft = null;
			const { api_key: _key, ...user } = result;
			void _key;
			if (!this.users.some((u) => u.id === user.id)) this.users = [...this.users, user];
			await this.load();
			return result.id;
		} catch (e) {
			this.draftError = (e as { detail?: string })?.detail || 'Failed to create API user';
			return null;
		} finally {
			this.creating = false;
		}
	}

	/**
	 * Save one change to an existing user. Returns true when the worker took
	 * it. A blank name is refused before any request.
	 */
	async update(id: string, patch: ApiUserUpdate): Promise<boolean> {
		if (patch.name !== undefined && !patch.name.trim()) {
			this.error = NAME_REQUIRED;
			return false;
		}
		const body: ApiUserUpdate = { ...patch };
		if (body.name !== undefined) body.name = body.name.trim();
		if (body.description !== undefined) body.description = body.description?.trim() || null;
		if (body.project_id !== undefined) body.project_id = body.project_id || null;
		if (body.model !== undefined) body.model = body.model || null;
		if (body.effort !== undefined) body.effort = body.effort || null;
		const key = `${id}:${Object.keys(body).join(',')}`;
		this.saving[key] = true;
		try {
			const updated = await updateApiUser(id, body);
			this.users = this.users.map((u) => (u.id === id ? { ...u, ...(updated ?? body) } as ApiUser : u));
			return true;
		} catch (e) {
			this.error = (e as { detail?: string })?.detail || 'Failed to update API user';
			return false;
		} finally {
			delete this.saving[key];
		}
	}

	/** Model change: effort follows what the new model supports. */
	setModel(user: ApiUser, model: string): Promise<boolean> {
		const effort = effortForModel(model, user.effort ?? '');
		return this.update(user.id, { model, effort });
	}

	toggleProfile(user: ApiUser, profileId: string): Promise<boolean> {
		const ids = user.profile_ids ?? [];
		const next = ids.includes(profileId) ? ids.filter((x) => x !== profileId) : [...ids, profileId];
		return this.update(user.id, { profile_ids: next });
	}

	async remove(id: string): Promise<boolean> {
		try {
			await deleteApiUser(id);
			if (this.revealed?.userId === id) this.revealed = null;
			this.users = this.users.filter((u) => u.id !== id);
			await this.load();
			return true;
		} catch (e) {
			this.error = (e as { detail?: string })?.detail || 'Failed to delete API user';
			return false;
		}
	}

	async rotate(id: string): Promise<boolean> {
		try {
			const result = await rotateApiUserKey(id);
			this.revealed = { userId: result.id, key: result.api_key };
			this.copied = false;
			return true;
		} catch (e) {
			this.error = (e as { detail?: string })?.detail || 'Failed to rotate API key';
			return false;
		}
	}

	async copyKey(): Promise<void> {
		if (!this.revealed) return;
		try {
			await copyText(this.revealed.key);
			this.copied = true;
			clearTimeout(this.#copyTimer);
			this.#copyTimer = setTimeout(() => (this.copied = false), 2000);
		} catch {
			this.copied = false;
			this.error = COPY_ERROR_MESSAGE;
		}
	}

	dismissKey(): void {
		this.revealed = null;
		this.copied = false;
	}

	dispose(): void {
		clearTimeout(this.#copyTimer);
	}
}

