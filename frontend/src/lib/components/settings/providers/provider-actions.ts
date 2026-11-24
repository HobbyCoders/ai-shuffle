/**
 * Actions the provider list and details share: switches and starting a setup.
 */
import { getErrorMessage } from '$lib/api/client';
import { providers } from '$lib/stores/providers.svelte';
import { notify } from '$lib/stores/notifications';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';
import { providerDrafts } from './drafts.svelte';
import { CLAUDE_CLOUD_ITEM, type CredentialFlavor, type CredentialView, type ProviderView } from './providers-model';

/**
 * A credential row's switch: PATCH `{enabled}` for model_providers rows, the
 * fal flag for fal.ai, and for Claude:
 *
 * - a saved API key: on selects it for Claude API, off turns Claude API off
 *   (every saved key is kept);
 * - a subscription sign-in: on selects it (if saved) and runs Claude Code on
 *   the subscription, off turns the subscription off. Claude API is not
 *   affected either way;
 * - the cloud provider or gateway: off turns it off; on opens its page, where
 *   its warning is acknowledged ("Save and use for Claude").
 *
 * Resolves to the error message on failure, else null.
 */
export async function setCredentialEnabled(c: CredentialView, enabled: boolean): Promise<string | null> {
	try {
		if (c.account) {
			if (enabled) await providers.activateAnthropicAccount(c.account.id);
			else await providers.deactivateAnthropicAccount(c.account.id);
		} else if (c.providerId === 'claude' && c.flavor === 'cli') {
			if (enabled) {
				if (c.subscription && !c.subscription.active) await providers.activateClaudeSubscriptionAccount(c.subscription.id);
				if (providers.current.claude?.mode !== 'subscription') await providers.setClaudeMode('subscription');
			} else if (c.enabled) {
				await providers.setClaudeMode('off');
			} else return null;
		} else if (c.providerId === 'claude' && c.flavor === 'cloud') {
			if (enabled) {
				settingsNav.selectItem(CLAUDE_CLOUD_ITEM, 'providers');
				return null;
			}
			if (!c.enabled) return null;
			await providers.setClaudeMode('off');
		} else if (c.flavor === 'fal') await providers.setFalEnabled(enabled);
		else if (c.row) await providers.setProviderEnabled(c.row.id, enabled);
		else return null;
		flashSaved();
		return null;
	} catch (e) {
		// Returned too, so the control that was used can say it next to itself.
		const message = getErrorMessage(e, `Could not turn ${c.name} ${enabled ? 'on' : 'off'}`);
		notify(message, { severity: 'error' });
		return message;
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
