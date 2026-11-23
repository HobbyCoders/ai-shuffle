/**
 * PR watch limits as Settings > Usage > PR watch shows them: what a watched
 * PR's agent may do, and how much it may cost. One shared system setting.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	getPrWatchSettings,
	updatePrWatchSettings,
	type PrWatchLimits,
	type PrWatchSettings
} from '$lib/api/pr-watches';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';

/** Mirrors app/core/pr_watch.py DEFAULTS and LIMITS. */
export const PR_WATCH_DEFAULTS: PrWatchLimits = {
	mode: 'fix',
	max_wakes_per_day: 5,
	max_usd_per_day: 3,
	idle_days: 7,
	max_repeat_failures: 3,
	trusted_bots: []
};
export const PR_WATCH_BOUNDS = {
	max_wakes_per_day: [0, 50],
	max_usd_per_day: [0, 100],
	idle_days: [1, 60],
	max_repeat_failures: [1, 10]
} as const;

/** "coderabbitai[bot], @copilot" -> ["coderabbitai", "copilot"]. */
export function parseBots(text: string): string[] {
	const out: string[] = [];
	for (const part of text.split(/[\s,]+/)) {
		const login = part.trim().replace(/^@/, '').replace(/\[bot\]$/i, '').toLowerCase();
		if (login && !out.includes(login)) out.push(login);
	}
	return out;
}

export class PrWatchSettingsModel {
	settings = $state<PrWatchSettings | null>(null);
	loadError = $state('');
	forbidden = $state(false);
	saveError = $state('');
	busy = $state(false);

	get values(): PrWatchLimits {
		const s = this.settings;
		return s ? { ...(s.defaults ?? PR_WATCH_DEFAULTS), ...stripMeta(s) } : PR_WATCH_DEFAULTS;
	}

	async load(): Promise<void> {
		try {
			this.settings = await getPrWatchSettings();
			this.loadError = '';
			this.forbidden = false;
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.loadError = getErrorMessage(e, 'Could not read the PR watch settings.');
		}
	}

	async save(patch: Partial<PrWatchLimits>): Promise<boolean> {
		if (this.busy) return false;
		this.busy = true;
		this.saveError = '';
		try {
			this.settings = await updatePrWatchSettings(patch);
			flashSaved();
			return true;
		} catch (e) {
			this.saveError = getErrorMessage(e, 'Could not save the PR watch settings.');
			await this.load();
			return false;
		} finally {
			this.busy = false;
		}
	}
}

function stripMeta(s: PrWatchSettings): PrWatchLimits {
	return {
		mode: s.mode,
		max_wakes_per_day: s.max_wakes_per_day,
		max_usd_per_day: s.max_usd_per_day,
		idle_days: s.idle_days,
		max_repeat_failures: s.max_repeat_failures,
		trusted_bots: s.trusted_bots ?? []
	};
}
