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
import { CLAUDE_CLOUD_ITEM, type CredentialFlavor, type CredentialView, type ProviderView } from './providers-model';

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

/**
 * "Cloud" in the Claude mode control. Turning it on needs the warning
 * acknowledged, so this opens the cloud credential (or its setup) where the
 * warning is, instead of switching the mode here.
 */
export function openClaudeCloud(provider: Pick<ProviderView, 'id' | 'credentials'>): void {
	if (provider.credentials.some((c) => c.flavor === 'cloud' && !c.draft)) {
		settingsNav.selectItem(CLAUDE_CLOUD_ITEM, 'providers');
		return;
	}
	const draft = provider.credentials.find((c) => c.flavor === 'cloud' && c.draft);
	if (draft) settingsNav.selectItem(draft.item, 'providers');
	else startSetup(provider, 'cloud');
}

/**
 * A pick in any Claude mode control (the provider header and its detail).
 * "Cloud" never switches here and never sends the acknowledgement: it opens
 * the cloud credential or its setup, where the warning is. Resolves false
 * for "Cloud" so the control keeps showing the current mode.
 */
export function pickClaudeMode(
	provider: Pick<ProviderView, 'id' | 'credentials'>,
	mode: ClaudeAuthMode
): Promise<unknown> {
	if (mode !== 'cloud') return changeClaudeMode(mode);
	openClaudeCloud(provider);
	return Promise.resolve(false);
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
