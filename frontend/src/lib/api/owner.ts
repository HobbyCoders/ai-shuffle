/**
 * Owner password and "trusted LAN, no login" (backend: app/api/owner_login.py).
 */
import { api } from './client';

export interface OwnerStatus {
	/** An owner password exists on this worker. */
	password_set: boolean;
	/** This browser may create the first password (no password yet, direct local network). */
	setup_allowed: boolean;
	/** The blocking "set an owner password" step applies. */
	required: boolean;
	/** "Trusted LAN, no login" is on. */
	trusted_lan: boolean;
	/** This browser can sign in with the password here. */
	login_available: boolean;
	/** The worker was started by the desktop app. */
	desktop_handoff: boolean;
	min_length: number;
}

export function isOwnerStatus(value: unknown): value is OwnerStatus {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	return ['password_set', 'setup_allowed', 'required', 'trusted_lan', 'login_available']
		.every((key) => typeof v[key] === 'boolean');
}

/** Null when the worker does not answer or predates owner passwords. */
export async function getOwnerStatus(): Promise<OwnerStatus | null> {
	try {
		const status = await api.get<unknown>('/owner/status', { cache: 'no-store' });
		return isOwnerStatus(status) ? status : null;
	} catch {
		return null;
	}
}

export function createOwnerPassword(password: string): Promise<{ status: string }> {
	return api.post('/owner/password', { password });
}

export function changeOwnerPassword(currentPassword: string, password: string): Promise<{ status: string }> {
	return api.post('/owner/password', { password, current_password: currentPassword });
}

export function ownerLogin(password: string): Promise<{ status: string }> {
	return api.post('/owner/login', { password });
}

export function setTrustedLan(enabled: boolean): Promise<{ trusted_lan: boolean }> {
	return api.put('/owner/trusted-lan', { enabled });
}
