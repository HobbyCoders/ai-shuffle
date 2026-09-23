/**
 * License status for this worker (backend: app/api/license.py).
 *
 * An unlicensed worker only refuses to start new agent runs; sessions, files,
 * settings and exports keep working. In beta builds the status is always
 * `mode: 'beta'` and nothing needs a key.
 */
import { api } from './client';

export type LicenseState =
	| 'beta'
	| 'active'
	| 'trialing'
	| 'grace'
	| 'expiring'
	| 'ending'
	| 'expired'
	| 'unlicensed'
	| 'invalid';

export interface LicenseBannerInfo {
	severity: 'warning' | 'error';
	message: string;
}

export interface LicenseStatus {
	mode: 'beta' | 'licensed';
	state: LicenseState;
	allowed: boolean;
	reason: string | null;
	banner: LicenseBannerInfo | null;
	plan: string | null;
	seats: number | null;
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
	server_configured: boolean;
}

export interface SeatHolder {
	install_id: string;
	name: string | null;
	app_version: string | null;
	last_seen: string;
}

export type ActivateResult =
	| ({ activated: true } & LicenseStatus)
	| { activated: false; code: 'seats_full'; detail: string; seats: number; installs: SeatHolder[] };

/** Fired on window after the license changes, so the banner re-reads it. */
export const LICENSE_CHANGED_EVENT = 'license-changed';

export const getLicense = () => api.get<LicenseStatus>('/license');
export const activateLicense = (key: string) => api.post<ActivateResult>('/license/activate', { key });
export const releaseSeat = (key: string, installId: string) =>
	api.post<{ ok: boolean; seats_used: number | null }>('/license/release', { key, install_id: installId });
export const deactivateLicense = () => api.post<LicenseStatus & { seat_released: boolean }>('/license/deactivate');
export const refreshLicense = () => api.post<LicenseStatus>('/license/refresh');

const PLAN_NAMES: Record<string, string> = {
	beta: 'Beta',
	trial: 'Trial',
	founder: 'Founder',
	monthly: 'Monthly',
	yearly: 'Yearly',
	founder_monthly: 'Founder, monthly',
	founder_yearly: 'Founder, yearly',
};

export function planName(plan: string | null): string {
	if (!plan) return 'None';
	return PLAN_NAMES[plan] ?? plan;
}
