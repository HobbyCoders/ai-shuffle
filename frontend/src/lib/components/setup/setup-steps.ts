/**
 * The first-run wizard's steps, shared by the /setup page and the owner
 * password gate (which shows the first two steps before a browser can sign in).
 */
import type { OwnerStatus } from '$lib/api/owner';

export type SetupStepId =
	| 'welcome'
	| 'owner'
	| 'license'
	| 'mode'
	| 'credential'
	| 'workspace'
	| 'first-run'
	| 'extras';

export interface SetupStep {
	id: SetupStepId;
	label: string;
}

const LABELS: Record<SetupStepId, string> = {
	welcome: 'Welcome',
	owner: 'Password',
	license: 'License',
	mode: 'Mode',
	credential: 'Sign in',
	workspace: 'Workspace',
	'first-run': 'First run',
	extras: 'Account & GitHub',
};

/**
 * Whether this install has an owner-password step. The desktop app signs its
 * own window in and never asks for one; neither does a worker whose owner
 * turned on "Trusted LAN, no login" before setting a password.
 */
export function hasOwnerStep(status: OwnerStatus | null | undefined): boolean {
	if (!status || status.desktop_handoff) return false;
	return status.required || status.password_set;
}

export function setupSteps(options: { ownerStep: boolean }): SetupStep[] {
	const ids: SetupStepId[] = ['welcome'];
	if (options.ownerStep) ids.push('owner');
	ids.push('license', 'mode', 'credential', 'workspace', 'first-run', 'extras');
	return ids.map((id) => ({ id, label: LABELS[id] }));
}

// The welcome screen is shown once per visit: by the owner gate before the
// password exists, or by /setup otherwise. Session storage only; losing it
// just shows the welcome again.
const WELCOME_KEY = 'ai-shuffle.setup.welcome-seen';

export function welcomeSeen(): boolean {
	try {
		return sessionStorage.getItem(WELCOME_KEY) === '1';
	} catch {
		return false;
	}
}

export function markWelcomeSeen(): void {
	try {
		sessionStorage.setItem(WELCOME_KEY, '1');
	} catch {
		// Private windows may refuse storage; the welcome shows again, harmlessly.
	}
}

/** Where to read about each disclosure. */
export const PRIVACY_UPDATES_URL = 'https://hobbycoders.com/privacy#updates';
