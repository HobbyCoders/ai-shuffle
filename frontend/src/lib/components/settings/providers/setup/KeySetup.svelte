<script lang="ts">
	/**
	 * KeySetup: a new API key.
	 *  - OpenAI, Grok and OpenRouter (`key`): Continue (`submitLabel`) creates the row with the
	 *    provider's fixed endpoint, then the models step picks what to offer. A
	 *    created row is reused on retry, never created twice; the retry's key
	 *    replaces the stored one (the provider may have rejected the first).
	 *  - Anthropic (`anthropic`): saves a named account and selects it for API key
	 *    mode, without changing the Claude Code mode.
	 */
	import { getErrorMessage } from '$lib/api/client';
	import { createModelProvider, updateModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';
	import ModelCuration from '../ModelCuration.svelte';
	import { providerDrafts, type Draft } from '../drafts.svelte';
	import { claudeKeyItem, KEY_CONFIG, rowItem } from '../providers-model';

	interface Props {
		draft: Draft;
		/** Label of the submit button for a non-Anthropic key (the setup wizard says "Save key"). */
		submitLabel?: string;
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { draft, submitLabel = 'Continue', onFinish, onCancel }: Props = $props();

	const config = $derived(
		draft.providerId === 'openai' || draft.providerId === 'grok' || draft.providerId === 'openrouter'
			? KEY_CONFIG[draft.providerId]
			: null
	);
	const placeholder = $derived(draft.flavor === 'anthropic' ? 'sk-ant-…' : (config?.placeholder ?? ''));

	let key = $state('');
	let label = $state('');
	let busy = $state(false);
	let error = $state('');

	async function submit() {
		const value = key.trim();
		if (!value || busy) return;
		busy = true;
		error = '';
		try {
			if (draft.flavor === 'anthropic') {
				const id = await providers.addAnthropicAccount(value, label);
				key = '';
				if (!id) throw new Error('Saved account could not be selected. Reopen Providers to try again.');
				onFinish(claudeKeyItem(id));
				return;
			}
			if (!config) return;
			// Keep the row on a refresh failure so a retry never creates a duplicate.
			let rowId = draft.rowId;
			if (!rowId) {
				const row = await createModelProvider({
					name: draft.name,
					provider_type: config.provider_type,
					base_url: config.base_url,
					auth_token: value
				});
				rowId = row.id;
				providerDrafts.update(draft.item, { rowId });
			} else {
				// Same row, new key: never a second row for one credential.
				await updateModelProvider(rowId, { auth_token: value });
			}
			await providers.refreshProviders();
			key = '';
			providerDrafts.update(draft.item, { step: 'models' });
		} catch (e) {
			error = getErrorMessage(
				e,
				draft.flavor === 'anthropic' ? 'Save failed' : `Could not add ${config?.name ?? 'the'} provider`
			);
		} finally {
			busy = false;
		}
	}
</script>

{#if draft.step === 'models' && draft.rowId}
	<ModelCuration mode="setup" providerId={draft.rowId} onDone={() => onFinish(rowItem(draft.rowId!))} />
{:else}
	<p class="pv-note">{draft.flavor === 'anthropic'
		? 'This becomes the selected Anthropic key. The Claude Code mode stays unchanged.'
		: 'This key becomes active. Other saved API keys for this provider turn off; subscriptions stay as they are.'}</p>
	<RowGroup title="Key">
		{#if draft.flavor === 'anthropic'}
			<SettingRow label="Name" hint="Optional, to tell your accounts apart">
				<TextField bind:value={label} placeholder="e.g. Work" maxlength={120} width={240} disabled={busy} />
			</SettingRow>
		{/if}
		<SettingRow label="API key" hint={draft.flavor === 'anthropic' ? 'Pay per use' : undefined}>
			<TextField bind:value={key} type="password" mono {placeholder} width={240} disabled={busy} />
		</SettingRow>
	</RowGroup>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" disabled={busy} onclick={onCancel}>Cancel</button>
		<button type="button" class="st-btn primary" disabled={busy || !key.trim()} onclick={submit}>
			{#if busy}Saving{:else if draft.flavor === 'anthropic'}Use this key{:else}{submitLabel}{/if}
		</button>
	</div>
{/if}
