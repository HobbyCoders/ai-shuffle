/**
 * Google Antigravity provider API (`/api/v1/antigravity`, admin only).
 * Mirrors `app/api/antigravity.py`.
 *
 * The agent is Google's own `agy` binary on the worker. Its stored sign-in
 * never crosses this API: sign-in runs the binary's own flow, and only the
 * link it prints and the code the owner pastes pass through here. A Gemini
 * API key is write-only, like every other provider key.
 */

import { api } from './client';
import type { ModelProvider, ProviderModelEntry } from './model-providers';

export type AntigravityAuthMode = 'oauth-personal' | 'oauth-business' | 'gemini-api-key';

export interface AntigravityAuthModeMeta {
	id: AntigravityAuthMode;
	label: string;
	/** The worker can run this sign-in without a browser of its own. */
	headless: boolean;
	/** Rows of this kind run only in chats the owner types into, unless lifted. */
	guarded: boolean;
}

export interface AntigravityMeta {
	provider_type: 'antigravity';
	capability: string;
	auth_modes: AntigravityAuthModeMeta[];
	dashboard_url: string;
	guard_default: boolean;
	default_models: ProviderModelEntry[];
}

/** The worker's one install / Check for update job. */
export interface AntigravityInstallJob {
	kind: 'install' | 'update' | string;
	state: 'idle' | 'running' | 'succeeded' | 'failed' | string;
	version?: string | null;
	error?: string | null;
	/** What finished: "Antigravity is up to date (1.5.7).", "… updated from X to Y.", or the failure. */
	message?: string | null;
}

/** The worker's start-time check for a newer Antigravity. It only asks. */
export interface AntigravityUpdateCheck {
	check_updates: boolean;
	/** The newer version that check found, or null. */
	latest_version: string | null;
}

export interface AntigravityInstallStatus {
	installed: boolean;
	version: string | null;
	/** `app` (installed by AI Shuffle), `path` (an agy on PATH), `override` (env var), or null. */
	source?: 'app' | 'path' | 'override' | null;
	/** Google publishes a build for this worker's platform. */
	can_install?: boolean;
	/** Why Install is unavailable when `can_install` is false; otherwise null. */
	unsupported_message?: string | null;
	install: AntigravityInstallJob;
	/** The start-time update check. Missing on a worker too old to have it. */
	check_updates?: boolean;
	latest_version?: string | null;
	/** Peer workers that must update before a row can be added. */
	workers_requiring_update: string[];
}

export interface AntigravityGuardView {
	guard_applies: boolean;
	allow_unattended: boolean;
	guarded: boolean;
}

export type AntigravityState = 'signed_in' | 'signed_out' | 'not_installed' | 'error';

export interface AntigravityProviderStatus extends AntigravityGuardView {
	state: AntigravityState;
	message?: string | null;
	agent?: Record<string, unknown> | null;
	auth_mode: AntigravityAuthMode;
	/** A sign-in waiting for its code, started on any device, or null. */
	login?: AntigravityLogin | null;
}

export interface AntigravityLogin {
	login_id?: string | null;
	provider_id?: string;
	/** `completing`: a code is being checked (any tab). `ended`: from the
	 *  state read only, the sign-in was finished, cancelled or expired. */
	state: 'awaiting_code' | 'completing' | 'ended' | 'failed' | 'not_installed' | 'signed_in';
	url?: string | null;
	message?: string | null;
}

export interface AntigravityCreate {
	name: string;
	auth_mode: AntigravityAuthMode;
	api_key?: string;
	models?: ProviderModelEntry[];
	default_model?: string;
	enabled?: boolean;
}

const BASE = '/antigravity';

export const getAntigravityMeta = (): Promise<AntigravityMeta> => api.get(`${BASE}/meta`);
export const getAntigravityStatus = (): Promise<AntigravityInstallStatus> => api.get(`${BASE}/status`);
export const installAntigravity = (): Promise<AntigravityInstallStatus['install']> => api.post(`${BASE}/install`);
/** Check for update: the same job; installs Google's newer release only when there is one. */
export const updateAntigravity = (): Promise<AntigravityInstallJob> => api.post(`${BASE}/update`);
/** Turn the worker's start-time check for a newer Antigravity on or off. */
export const setAntigravityUpdateCheck = (check_updates: boolean): Promise<AntigravityUpdateCheck> =>
	api.put(`${BASE}/update-check`, { check_updates });
export const createAntigravityProvider = (body: AntigravityCreate): Promise<ModelProvider & AntigravityGuardView> =>
	api.post(`${BASE}/providers`, body);
export const getAntigravityProviderStatus = (id: string): Promise<AntigravityProviderStatus> =>
	api.get(`${BASE}/providers/${id}/status`);
export const setAntigravityGuard = (id: string, allowUnattended: boolean): Promise<AntigravityGuardView> =>
	api.put(`${BASE}/providers/${id}/guard`, { allow_unattended: allowUnattended });
export const startAntigravityLogin = (id: string): Promise<AntigravityLogin> => api.post(`${BASE}/providers/${id}/login`);
export const completeAntigravityLogin = (id: string, loginId: string, code: string): Promise<AntigravityLogin> =>
	api.post(`${BASE}/providers/${id}/login/${loginId}/complete`, { code });
/** One sign-in's state, from the worker's memory: no agent run, cheap to poll (#1533). */
export const getAntigravityLogin = (id: string, loginId: string): Promise<AntigravityLogin> =>
	api.get(`${BASE}/providers/${id}/login/${loginId}`);
export const cancelAntigravityLogin = (id: string, loginId: string): Promise<unknown> =>
	api.delete(`${BASE}/providers/${id}/login/${loginId}`);
export const signOutAntigravity = (id: string): Promise<{ state: 'signed_out' }> =>
	api.post(`${BASE}/providers/${id}/sign-out`);
