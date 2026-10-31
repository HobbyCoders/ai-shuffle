/**
 * The New project dialog's last "Set it up on" choice.
 *
 * Stored as the workers the owner UNticked, so a worker added later starts
 * ticked (decision 3: every online worker pre-ticked for a Git repo).
 * Account-synced through the preference store (`projects.setup_targets`)
 * with a localStorage copy for an instant first paint and for sessions that
 * may not write preferences.
 */

import { preferenceSync } from '$lib/services/preferences';

const PREFERENCE_KEY = 'projects.setup_targets';
const LOCAL_KEY = 'ai-shuffle:projects.setup_targets';

function parse(value: unknown): string[] {
	const list = (value as { unticked?: unknown } | null)?.unticked;
	return Array.isArray(list) ? list.filter((x): x is string => typeof x === 'string') : [];
}

function readLocal(): string[] {
	try {
		return parse(JSON.parse(localStorage.getItem(LOCAL_KEY) ?? 'null'));
	} catch {
		return [];
	}
}

function writeLocal(unticked: string[]) {
	try {
		localStorage.setItem(LOCAL_KEY, JSON.stringify({ unticked }));
	} catch {
		/* best effort */
	}
}

let unticked = $state<string[]>(typeof localStorage === 'undefined' ? [] : readLocal());

preferenceSync.register(PREFERENCE_KEY, {
	read: () => ({ unticked: [...unticked] }),
	apply(value) {
		// undefined = reset (sign-out); keep the device's own copy then.
		if (value === undefined) return;
		unticked = parse(value);
		writeLocal(unticked);
	}
});

export const projectSetupTargets = {
	/** Install ids the owner left unticked last time. */
	get unticked(): readonly string[] {
		return unticked;
	},
	remember(next: string[]) {
		unticked = [...new Set(next)];
		writeLocal(unticked);
		preferenceSync.save(PREFERENCE_KEY, { unticked });
	}
};
