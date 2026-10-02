<script lang="ts">
	/**
	 * GatewaySetup: "Add provider". A custom gateway is its own provider and its
	 * own group in the model picker: name, endpoint, the dialect it speaks
	 * (Anthropic Messages or OpenAI chat/completions) and an optional token.
	 * Connect creates the row (`provider_type: 'custom'`), then the models step
	 * picks what to offer.
	 */
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';
	import Segmented from '../../rows/Segmented.svelte';
	import ModelCuration from '../ModelCuration.svelte';
	import { providerDrafts, type Draft } from '../drafts.svelte';
	import { rowItem } from '../providers-model';

	interface Props {
		draft: Draft;
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { draft, onFinish, onCancel }: Props = $props();

	type Dialect = 'anthropic' | 'openai';
	const DIALECTS: { value: Dialect; label: string }[] = [
		{ value: 'anthropic', label: 'Anthropic Messages' },
		{ value: 'openai', label: 'OpenAI chat' }
	];

	let name = $state('');
	let endpoint = $state('');
	let dialect = $state<Dialect>('openai');
	let token = $state('');
	let busy = $state(false);
	let error = $state('');

	async function connect() {
		if (busy) return;
		if (!name.trim() || !endpoint.trim()) {
			error = 'Name and endpoint are both required.';
			return;
		}
		busy = true;
		error = '';
		try {
			const row = await providers.createGateway({ name, base_url: endpoint, api_dialect: dialect, auth_token: token });
			token = '';
			providerDrafts.update(draft.item, { rowId: row.id, step: 'models' });
		} catch (e) {
			error = getErrorMessage(e, 'Could not add the gateway');
		} finally {
			busy = false;
		}
	}
</script>

{#if draft.step === 'models' && draft.rowId}
	<ModelCuration mode="setup" providerId={draft.rowId} onDone={() => onFinish(rowItem(draft.rowId!))} />
{:else}
	<RowGroup title="Custom gateway">
		<SettingRow label="Name">
			<TextField
				bind:value={name}
				placeholder="Together AI"
				width={240}
				disabled={busy}
				oninput={(v) => providerDrafts.renameGateway(draft.providerId, v)}
			/>
		</SettingRow>
		<SettingRow label="Endpoint">
			<TextField bind:value={endpoint} type="url" mono placeholder="https://host/v1" width={240} disabled={busy} />
		</SettingRow>
		<SettingRow label="API dialect">
			<Segmented options={DIALECTS} bind:value={dialect} disabled={busy} />
		</SettingRow>
		<SettingRow label="Auth token" hint="Optional">
			<TextField bind:value={token} type="password" mono placeholder="Optional" width={240} disabled={busy} />
		</SettingRow>
	</RowGroup>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" disabled={busy} onclick={onCancel}>Cancel</button>
		<button type="button" class="st-btn primary" disabled={busy} onclick={connect}>{busy ? 'Connecting' : 'Connect'}</button>
	</div>
{/if}
