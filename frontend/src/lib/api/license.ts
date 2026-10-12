/**
 * AI Shuffle Cloud status for this worker (backend: app/api/license.py).
 *
 * The app is free to run. A Cloud account only decides how many remote workers
 * the hub serves: none without an account, one on a free account, the plan's
 * seats on a paid one. Nothing here can stop a local agent run, so `allowed`
 * is always true and `banner` always null; both stay for older callers.
 * The routes keep their `/license` names for compatibility.
 */
import { api } from './client';

/** What the account gives this worker. */
export type CloudState = 'not_linked' | 'free' | 'paid';

/** The signed paid-plan lease, if any. Only a paid plan has one. */
export type LeaseState = 'none' | 'active' | 'grace' | 'expiring' | 'ending' | 'expired' | 'invalid' | 'seat_lost';

export interface LicenseStatus {
	mode: 'cloud';
	state: CloudState;
	/** The worker's stored Cloud account binding; independent of a paid key. */
	linked: boolean;
	remote_worker_limit: number;
	lease_state: LeaseState;
	/** One Settings-only line that describes the state. */
	message: string | null;
	allowed: true;
	reason: null;
	banner: null;
	plan: string | null;
	seats: number | null;
	/** Seats in use when the lease was signed; null on free and unlinked. */
	seats_used: number | null;
	account_id: string | null;
	license_id: string | null;
	key_hint: string | null;
	install_id: string | null;
	issued_at: string | null;
	expires_at: string | null;
	grace: boolean;
	grace_until: string | null;
	last_refresh_at: string | null;
	next_refresh_at: string | null;
	last_error: string | null;
	server_status: { code: string; message: string } | null;
	/** False in builds without a lease server: no paid key entry. */
	server_configured: boolean;
}

export interface SeatHolder {
	install_id: string;
	name: string | null;
	app_version?: string | null;
	last_seen: string;
}

export type ActivateResult =
	| ({ activated: true } & LicenseStatus)
	| { activated: false; code: 'seats_full'; detail: string; seats: number; installs: SeatHolder[] };

export const getLicense = () => api.get<LicenseStatus>('/license');
export const activateLicense = (key: string) => api.post<ActivateResult>('/license/activate', { key });
export const releaseSeat = (key: string, installId: string) =>
	api.post<{ ok: boolean; seats_used: number | null }>('/license/release', { key, install_id: installId });
export const deactivateLicense = () => api.post<LicenseStatus & { seat_released: boolean }>('/license/deactivate');
export const refreshLicense = () => api.post<LicenseStatus>('/license/refresh');

/** Public pricing page for the paid plan. */
export const CLOUD_PRICING_URL = 'https://hobbycoders.com/ai-shuffle/pricing';
/** In-app docs page (Docs panel deep link). */
export const CLOUD_DOCS = 'getting-started/cloud';

const PLAN_NAMES: Record<string, string> = {
	free: 'Free',
	beta: 'Beta',
	founder: 'Founder',
	monthly: 'Monthly',
	yearly: 'Yearly',
	founder_monthly: 'Founder, monthly',
	founder_yearly: 'Founder, yearly'
};

export function planName(plan: string | null): string {
	if (!plan) return 'None';
	return PLAN_NAMES[plan] ?? plan;
}
