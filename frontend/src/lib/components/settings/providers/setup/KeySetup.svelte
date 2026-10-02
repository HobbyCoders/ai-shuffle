<script lang="ts">
	/**
	 * KeySetup: a new API key.
	 *  - OpenAI, Grok and OpenRouter (`key`): Continue creates the row with the
	 *    provider's fixed endpoint, then the models step picks what to offer. A
	 *    created row is reused on retry, never created twice.
	 *  - Anthropic (`anthropic`): "Use this key" stores it for Claude Code. When
	 *    Claude has no other credential it also becomes the active one.
	 */
	import { getErrorMessage } from '$lib/api/client';
	import { createModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';
	import ModelCuration from '../ModelCuration.svelte';
	import { providerDrafts, type Draft } from '../drafts.svelte';
	import { CLAUDE_KEY_ITEM, KEY_CONFIG, rowItem } from '../providers-model';

	interface Props {
		draft: Draft;
		/** Claude has no other credential: the new key becomes the active one. */
		makeActive?: boolean;
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { draft, makeActive = false, onFinish, onCancel }: Props = $props();

	const config = $derived(
		draft.providerId === 'openai' || draft.providerId === 'grok' || draft.providerId === 'openrouter'
			? KEY_CONFIG[draft.providerId]
			: null
	);
	const placeholder = $derived(draft.flavor === 'anthropic' ? 'sk-ant-…' : (config?.placeholder ?? ''));

	let key = $state('');
	let busy = $state(false);
	let error = $state('');

	async function submit() {
		const value = key.trim();
		if (!value || busy) return;
		busy = true;
		error = '';
		try {
			if (draft.flavor === 'anthropic') {
				await providers.setAnthropicKey(value, { useForClaudeCode: makeActive });
				await providers.loadClaude();
				key = '';
				onFinish(CLAUDE_KEY_ITEM);
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
	<RowGroup title="Key">
		<SettingRow label="API key" hint={draft.flavor === 'anthropic' ? 'Pay per use' : undefined}>
			<TextField bind:value={key} type="password" mono {placeholder} width={240} disabled={busy} />
		</SettingRow>
	</RowGroup>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" disabled={busy} onclick={onCancel}>Cancel</button>
		<button type="button" class="st-btn primary" disabled={busy || !key.trim()} onclick={submit}>
			{#if busy}Saving{:else if draft.flavor === 'anthropic'}Use this key{:else}Continue{/if}
		</button>
	</div>
{/if}
