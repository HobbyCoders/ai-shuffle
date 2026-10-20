/**
 * AI Shuffle Cloud status for this worker (backend: app/api/license.py).
 *
 * The app is free to run and has no license key. The plan comes from the
 * Cloud account this worker is signed in with (`source: 'account'`), and it
 * only decides how many remote workers the hub serves: none without an
 * account, two on a free account, four on a paid plan by default (up to the
 * plan's seat count). Nothing here can stop a local agent run, so `allowed`
 * is always true and `banner` always null; both stay for older callers.
 * The route keeps its `/license` name for compatibility.
 */
import { api } from './client';

/** What the account gives this worker. */
export type CloudState = 'not_linked' | 'free' | 'paid';

export interface LicenseStatus {
	mode: 'cloud';
	/** Where the plan comes from: always the signed-in account. Absent on older workers. */
	source?: 'account';
	state: CloudState;
	/** The worker's stored Cloud account binding. */
	linked: boolean;
	remote_worker_limit: number;
	/** One Settings-only line that describes the state. */
	message: string | null;
	allowed: true;
	reason: null;
	banner: null;
	plan: 'free' | 'paid' | null;
	/** The paid plan's seat count; null when the account does not say. */
	seats: number | null;
	/** Remote workers in use on the account; null when unknown. */
	seats_used: number | null;
	/** Paid renewal date (ISO), null otherwise. */
	expires_at: string | null;
	/* Kept by the backend for older callers, always empty now. */
	key_hint: null;
	install_id: null;
	lease_state: 'none';
	server_configured: false;
}

export const getLicense = () => api.get<LicenseStatus>('/license');

/** Public pricing page for the paid plan. */
export const CLOUD_PRICING_URL = 'https://hobbycoders.com/ai-shuffle/pricing';
/** In-app docs page (Docs panel deep link). */
export const CLOUD_DOCS = 'getting-started/cloud';

/** Remote workers on a free account. */
export const FREE_WORKERS = 2;
/** Remote workers on the paid plan when the account gives no seat count. */
export const PAID_WORKERS = 4;
/** Seat counts from here up read as unlimited. */
export const UNLIMITED_SEATS = 100;
