/**
 * Actions the provider list and details share: switches, the Claude mode,
 * and starting a setup.
 */
import { getErrorMessage } from '$lib/api/client';
import type { ClaudeAuthMode } from '$lib/api/claude-auth';
import { providers } from '$lib/stores/providers.svelte';
import { notify } from '$lib/stores/notifications';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';
import { providerDrafts } from './drafts.svelte';
import type { CredentialFlavor, CredentialView, ProviderView } from './providers-model';

/** A credential row's switch: PATCH `{enabled}` for model_providers rows, the fal flag for fal.ai. */
export async function setCredentialEnabled(c: CredentialView, enabled: boolean): Promise<void> {
	try {
		if (c.flavor === 'fal') await providers.setFalEnabled(enabled);
		else if (c.row) await providers.setProviderEnabled(c.row.id, enabled);
		else return;
		flashSaved();
	} catch (e) {
		notify(getErrorMessage(e, `Could not turn ${c.name} ${enabled ? 'on' : 'off'}`), { severity: 'error' });
	}
}

/** Claude's Subscription | API key | Off. */
export async function changeClaudeMode(mode: ClaudeAuthMode): Promise<void> {
	if (providers.current.claude?.mode === mode) return;
	try {
		await providers.setClaudeMode(mode);
		flashSaved();
	} catch (e) {
		notify(getErrorMessage(e, 'Could not change the Claude Code mode'), { severity: 'error' });
	}
}

/** One draft row for a kind, selected so its setup shows in the detail. */
export function startSetup(provider: Pick<ProviderView, 'id' | 'credentials'>, flavor: CredentialFlavor): void {
	const draft = providerDrafts.add(provider.id, flavor, provider.credentials);
	settingsNav.selectItem(draft.item, 'providers');
}

/** "Add provider": a new custom gateway. */
export function startGateway(): void {
	const draft = providerDrafts.addGateway();
	settingsNav.selectItem(draft.item, 'providers');
}
