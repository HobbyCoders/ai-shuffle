/**
 * Read-only mode. The only web viewer without owner rights is the owner's own
 * paired device that is not approved yet (`is_admin: false`). Settings then
 * shows everything read-only under one approval banner: primitives disable
 * themselves, DetailPane disables its body, and 403s on load become a quiet
 * "Available after approval" state.
 */
import { getContext, setContext } from 'svelte';
import { fromStore } from 'svelte/store';
import { isAdmin } from '$lib/stores/auth.svelte';

export interface SettingsAccess {
	readonly readOnly: boolean;
}

/** Context key; exported for test harnesses. */
export const SETTINGS_ACCESS_CONTEXT = Symbol('settings-access');
const KEY = SETTINGS_ACCESS_CONTEXT;
const admin = fromStore(isAdmin);

/** Live access from the auth store. SettingsShell puts this in context. */
export const settingsAccess: SettingsAccess = {
	get readOnly() {
		return !admin.current;
	}
};

export function setSettingsAccess(access: SettingsAccess = settingsAccess): void {
	setContext(KEY, access);
}

const WRITABLE: SettingsAccess = { readOnly: false };

/**
 * Access for a component inside Settings. Outside the shell (unit tests,
 * reuse elsewhere) it is writable.
 */
export function getSettingsAccess(): SettingsAccess {
	return getContext<SettingsAccess | undefined>(KEY) ?? WRITABLE;
}

export type LoadState<T> =
	| { status: 'ok'; data: T }
	| { status: 'forbidden' }
	| { status: 'error'; message: string };

export function isForbidden(err: unknown): boolean {
	return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 403;
}

function errorMessage(err: unknown): string {
	if (typeof err === 'object' && err !== null) {
		const detail = (err as { detail?: unknown; message?: unknown }).detail ?? (err as { message?: unknown }).message;
		if (typeof detail === 'string' && detail) return detail;
	}
	return typeof err === 'string' && err ? err : 'Could not load';
}

/**
 * Run a settings load. A 403 (unapproved device) is `forbidden`, not an
 * error: render "Available after approval" and log nothing.
 */
export async function loadState<T>(load: () => Promise<T>): Promise<LoadState<T>> {
	try {
		return { status: 'ok', data: await load() };
	} catch (err) {
		if (isForbidden(err)) return { status: 'forbidden' };
		return { status: 'error', message: errorMessage(err) };
	}
}

export const FORBIDDEN_TEXT = 'Available after approval';
