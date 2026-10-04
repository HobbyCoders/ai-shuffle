/**
 * Plugin service sign-in (OAuth) API client: `/api/v1/plugins/oauth/*`
 * (app/api/plugin_oauth.py). No route returns a client secret, a code or a
 * token, and nothing here stores one.
 */
import { api } from './client';

/** `state` of one plugin service's sign-in. */
export type SignInState = 'not_configured' | 'signed_out' | 'signed_in' | 'needs_sign_in' | 'invalid';

/** Where the OAuth client comes from; absent on workers that predate the field. */
export type ClientSource = 'owner' | 'plugin' | 'registered' | 'none';

/**
 * Whether a sign-in returns through the account hub's fixed address
 * (`relay_uri`). Only a client the plugin ships uses it: `not_used` for
 * every other source, `unavailable` when this worker can't use it.
 */
export type RelayUse = 'used' | 'not_used' | 'unavailable';

/** Why the relay is `unavailable`, checked in this order; `''` otherwise. */
export type RelayReason = 'not_linked' | 'insecure_address' | 'not_configured' | '';

export interface ServiceSignIn {
	plugin_id: string;
	service: string;
	state: SignInState;
	/**
	 * For `needs_sign_in`: scopes_widened, endpoint_changed, client_changed,
	 * expired or unreadable. For `not_configured`: registration_failed when
	 * automatic client registration did not work.
	 */
	reason: string;
	/** The owner's own client. */
	client_configured: boolean;
	client_id: string;
	client_source?: ClientSource;
	/** A client the plugin ships, for display. */
	plugin_client_id?: string;
	/** Where the worker registers a client automatically, for display. */
	registration_url?: string;
	requested_scopes?: string[];
	scopes?: string[];
	missing_scopes?: string[];
	expires_at?: number | null;
	can_refresh?: boolean;
	/** Declared access: `read` (GET and HEAD) or `write`. */
	access?: 'read' | 'write';
	authorize_host?: string;
	/** Where the provider sends the browser back: `relay_uri` when the relay is used, else the worker's own address. */
	return_address?: string;
	/** Absent on workers that predate the relay: the screens behave as before. */
	relay?: RelayUse;
	relay_reason?: RelayReason;
}

export interface PluginSignInStatus {
	plugin_id: string;
	/** Switched on and approved. */
	enabled: boolean;
	/** This worker's return address as the current browser reaches it. */
	redirect_uri: string;
	/** The hub's fixed return address (`''` when it isn't configured); absent on older workers. */
	relay_uri?: string;
	/** Linked to an account and reached at an address the relay can return to. */
	relay_available?: boolean;
	services: ServiceSignIn[];
}

export interface SignInStart {
	authorization_url: string;
	flow_id: string;
	redirect_uri: string;
	expires_in: number;
	/** The sign-in returns through the hub (`redirect_uri` is then `relay_uri`); absent on older workers. */
	relayed?: boolean;
	/** The worker's own origin, where the hub sends the browser on to. */
	return_origin?: string;
}

/** What the callback page posts to the window that opened it. */
export interface SignInMessage {
	type: 'plugin-oauth';
	flow_id: string;
	plugin_id: string;
	service: string;
	state: SignInState | null;
	error: string | null;
}

const BASE = '/plugins/oauth';

export function getSignInStatus(plugin: string, service = ''): Promise<PluginSignInStatus> {
	const query = new URLSearchParams({ plugin });
	if (service) query.set('service', service);
	return api.get<PluginSignInStatus>(`${BASE}/status?${query}`);
}

export function startSignIn(plugin: string, service: string): Promise<SignInStart> {
	return api.post<SignInStart>(`${BASE}/start`, { plugin, service });
}

export function signOut(plugin: string, service: string): Promise<{ signed_out: boolean; revoked: boolean }> {
	return api.post(`${BASE}/sign-out`, { plugin, service });
}

/** Save the owner's OAuth client. `client_secret` is left out for a public client. */
export function saveClient(plugin: string, service: string, clientId: string, clientSecret = ''): Promise<ServiceSignIn> {
	const body: Record<string, string> = { plugin, service, client_id: clientId };
	if (clientSecret) body.client_secret = clientSecret;
	return api.put<ServiceSignIn>(`${BASE}/client`, body);
}

export async function deleteClient(plugin: string, service: string): Promise<void> {
	await api.delete(`${BASE}/client?${new URLSearchParams({ plugin, service })}`);
}
