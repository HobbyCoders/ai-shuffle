/**
 * Voice-mode API client (wave VW2 of docs/VOICE_MODE_PLAN.md).
 *
 * Two groups of endpoints live here:
 *
 * 1. **Shipped in VW1** — `GET/PUT /settings/voice`, `GET /voice/providers` and
 *    `POST /voice/sessions`. These are always available.
 * 2. **Owned by VW3 (the sideband bridge)** — `/voice/sessions/{id}/secret`,
 *    `/call`, `/hangup` and `/state`. VW2 is built against the pinned contract
 *    but must not break while VW3 is unmerged, so every one of those calls is
 *    wrapped in {@link optional}: a 404/405 (route not registered yet) resolves
 *    to `null` with a console warning instead of throwing. Real errors from a
 *    route that DOES exist still surface.
 *
 * The settings reader is memoised at module scope on purpose. Settings sections
 * remount on every tab switch and the new-chat Voice pill is evaluated on every
 * empty-state render — a per-mount fetch would both hammer the endpoint and
 * risk snapping a half-typed field back to the server value (the preference
 * store typing-race documented for the git-actions section). `force` is the
 * single escape hatch, used after a successful PUT.
 */

import { api, type ApiError } from './client';

// ============================================================================
// Types
// ============================================================================

/** The realtime models a voice session may run on (backend allowlist). */
export const VOICE_MODELS = [
	{
		id: 'gpt-realtime-2.1',
		label: 'gpt-realtime-2.1 (best, ~$0.05–0.45/min)',
	},
	{
		id: 'gpt-realtime-2.1-mini',
		label: 'gpt-realtime-2.1-mini (cheapest, ~$0.01–0.05/min)',
	},
	{
		id: 'gpt-realtime',
		label: 'gpt-realtime (legacy)',
	},
] as const;

export interface VoiceSettings {
	voice_mode_enabled: boolean;
	voice_model: string;
	voice_provider_id: string | null;
	voice_max_call_minutes: number;
}

/** Partial update. An explicit `null` provider clears the pinned provider. */
export interface VoiceSettingsUpdate {
	voice_mode_enabled?: boolean;
	voice_model?: string;
	voice_provider_id?: string | null;
	voice_max_call_minutes?: number;
}

/** A `model_providers` row eligible to fund a realtime call (VM3). */
export interface VoiceProvider {
	id: string;
	name: string;
}

export interface VoiceSessionCreated {
	session_id: string;
	client_secret: string;
	expires_at: number | null;
	model: string;
}

/** Re-mint for an existing voice session (VW3 contract). */
export interface VoiceSecret {
	client_secret: string;
	expires_at: number | null;
	model: string;
}

/** Server-side call state (VW3 contract; usage + cost added in VW5). */
export interface VoiceCallState {
	active: boolean;
	connected: boolean;
	started_at: number | null;
	model: string | null;
	/** Realtime token counters for the whole call (survives a reconnect). */
	usage: Record<string, number>;
	/** List-price estimate for `usage`, or null for an unpriced model. */
	estimated_cost_usd: number | null;
}

// ============================================================================
// VW3-contract helper
// ============================================================================

function isApiError(e: unknown): e is ApiError {
	return typeof e === 'object' && e !== null && 'status' in e;
}

/**
 * Run a call against a VW3-owned route, tolerating "not built yet".
 *
 * A 404/405 means the bridge router isn't registered on this build — warn and
 * resolve `null` so the call surface degrades instead of erroring. Anything
 * else is a real failure and is rethrown.
 */
async function optional<T>(what: string, run: () => Promise<T>): Promise<T | null> {
	try {
		return await run();
	} catch (e) {
		if (isApiError(e) && (e.status === 404 || e.status === 405)) {
			console.warn(`[voice] ${what}: backend route not available yet (VW3) — skipping.`);
			return null;
		}
		throw e;
	}
}

// ============================================================================
// Settings (VW1)
// ============================================================================

let settingsCache: Promise<VoiceSettings> | null = null;

/**
 * Read the voice settings, memoised for the lifetime of the page.
 *
 * @param force Re-fetch and replace the cache (used after a PUT).
 */
export function loadVoiceSettings(force = false): Promise<VoiceSettings> {
	if (force || !settingsCache) {
		settingsCache = api.get<VoiceSettings>('/settings/voice').catch((e) => {
			// Never poison the cache with a rejected promise — the next caller
			// should be able to retry.
			settingsCache = null;
			throw e;
		});
	}
	return settingsCache;
}

/** Partial update; the response is the full settled settings object. */
export async function saveVoiceSettings(patch: VoiceSettingsUpdate): Promise<VoiceSettings> {
	const updated = await api.put<VoiceSettings>('/settings/voice', patch);
	settingsCache = Promise.resolve(updated);
	return updated;
}

/**
 * Is voice mode enabled? Cached; never throws (a failed read reads as "off" so
 * the new-chat surface simply doesn't grow a Voice pill).
 */
export async function isVoiceEnabled(): Promise<boolean> {
	try {
		return (await loadVoiceSettings()).voice_mode_enabled;
	} catch (e) {
		console.warn('[voice] could not read voice settings:', e);
		return false;
	}
}

// ============================================================================
// Providers + session creation (VW1)
// ============================================================================

export function getVoiceProviders(): Promise<VoiceProvider[]> {
	return api.get<VoiceProvider[]>('/voice/providers');
}

export function createVoiceSession(body: {
	profile_id?: string | null;
	project_id?: string | null;
	provider_id?: string | null;
}): Promise<VoiceSessionCreated> {
	return api.post<VoiceSessionCreated>('/voice/sessions', body);
}

// ============================================================================
// Call lifecycle (VW3 contract — graceful while unmerged)
// ============================================================================

/** Re-mint a client secret for an existing voice session. */
export function mintVoiceSecret(sessionId: string): Promise<VoiceSecret | null> {
	return optional('mint secret', () =>
		api.post<VoiceSecret>(`/voice/sessions/${sessionId}/secret`)
	);
}

/** Hand the backend the OpenAI `call_id` so it can attach its sideband. */
export function attachVoiceCall(sessionId: string, callId: string): Promise<{ ok: boolean } | null> {
	return optional('attach call', () =>
		api.post<{ ok: boolean }>(`/voice/sessions/${sessionId}/call`, { call_id: callId })
	);
}

/** Tell the backend the user ended the call. */
export function hangupVoiceCall(sessionId: string): Promise<{ ok: boolean } | null> {
	return optional('hangup', () => api.post<{ ok: boolean }>(`/voice/sessions/${sessionId}/hangup`));
}

/** Server-side view of the call (unused by the happy path; handy for recovery). */
export function getVoiceCallState(sessionId: string): Promise<VoiceCallState | null> {
	return optional('call state', () =>
		api.get<VoiceCallState>(`/voice/sessions/${sessionId}/state`)
	);
}
