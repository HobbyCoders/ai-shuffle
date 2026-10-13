/**
 * The subscription loop guard, as the Claude subscription's Policy and Usage
 * tabs show it: a budget for unattended Claude turns (automations, wake-ups,
 * background workers, connectors) while Claude runs on the subscription, a
 * time limit for background workers, and the turns it holds for the owner.
 * It does nothing in API key mode or while Claude is off.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	confirmHeldTurn,
	dismissHeldTurn,
	getLoopGuard,
	updateLoopGuard,
	type LoopGuardSettings,
	type LoopGuardStatus
} from '$lib/api/loop-guard';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';

export const HELD_SOURCE_LABELS: Record<string, string> = {
	automation: 'Automation',
	wakeup: 'Scheduled wake-up',
	provider_agent: 'Background worker',
	connector: 'Connector'
};

export class LoopGuard {
	status = $state<LoopGuardStatus | null>(null);
	loadError = $state('');
	forbidden = $state(false);
	saveError = $state('');
	busy = $state(false);

	async load(): Promise<void> {
		try {
			this.status = await getLoopGuard();
			this.loadError = '';
			this.forbidden = false;
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.loadError = getErrorMessage(e, 'Could not read the loop guard settings.');
		}
	}

	async save(patch: Partial<LoopGuardSettings>): Promise<void> {
		if (this.busy) return;
		this.busy = true;
		this.saveError = '';
		try {
			this.status = await updateLoopGuard(patch);
			flashSaved();
		} catch (e) {
			this.saveError = getErrorMessage(e, 'Could not save the loop guard settings.');
			await this.load();
		} finally {
			this.busy = false;
		}
	}

	async act(id: string, action: 'confirm' | 'dismiss'): Promise<void> {
		if (this.busy) return;
		this.busy = true;
		this.saveError = '';
		try {
			if (action === 'confirm') await confirmHeldTurn(id);
			else await dismissHeldTurn(id);
		} catch (e) {
			this.saveError = getErrorMessage(e, 'Could not update the held turn.');
		} finally {
			this.busy = false;
			await this.load();
		}
	}
}

/** "Scheduled wake-up, 14:02". */
export function heldTitle(source: string, heldAt: number): string {
	const time = new Date(heldAt * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
	return `${HELD_SOURCE_LABELS[source] ?? source}, ${time}`;
}

/** The first five words of a held prompt, for the row hint. */
export function promptHint(prompt: string | null): string | undefined {
	const words = (prompt ?? '').trim().split(/\s+/).filter(Boolean);
	if (!words.length) return undefined;
	return words.slice(0, 5).join(' ') + (words.length > 5 ? '…' : '');
}
