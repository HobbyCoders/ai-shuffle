/**
 * The "Resume when the limit resets" and "Compact before resuming" switches
 * (#1831), as Settings > General > Usage limits shows them. One shared system
 * setting that applies to every provider.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	getLimitResume,
	updateLimitResume,
	type LimitResumeSettings,
	type LimitResumeSwitches
} from '$lib/api/limit-resume';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';

export const LIMIT_RESUME_DEFAULTS: LimitResumeSwitches = {
	resume_on_reset: false,
	compact_before_resume: true
};

export class LimitResume {
	settings = $state<LimitResumeSettings | null>(null);
	loadError = $state('');
	forbidden = $state(false);
	saveError = $state('');
	busy = $state(false);

	get switches(): LimitResumeSwitches {
		const s = this.settings;
		const fallback = s?.defaults ?? LIMIT_RESUME_DEFAULTS;
		return {
			resume_on_reset:
				typeof s?.resume_on_reset === 'boolean' ? s.resume_on_reset : fallback.resume_on_reset,
			compact_before_resume:
				typeof s?.compact_before_resume === 'boolean' ? s.compact_before_resume : fallback.compact_before_resume
		};
	}

	async load(): Promise<void> {
		try {
			this.settings = await getLimitResume();
			this.loadError = '';
			this.forbidden = false;
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.loadError = getErrorMessage(e, 'Could not read the usage limit settings.');
		}
	}

	async save(patch: Partial<LimitResumeSwitches>): Promise<void> {
		if (this.busy) return;
		this.busy = true;
		this.saveError = '';
		try {
			this.settings = await updateLimitResume(patch);
			flashSaved();
		} catch (e) {
			this.saveError = getErrorMessage(e, 'Could not save the usage limit settings.');
			await this.load();
		} finally {
			this.busy = false;
		}
	}
}
