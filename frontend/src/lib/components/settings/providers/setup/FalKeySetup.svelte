<script lang="ts">
	/**
	 * FalKeySetup: the fal.ai media key. fal checks it on save; the same key
	 * reads the account balance. Media models are chosen in Default models.
	 */
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';
	import { FAL_KEY_ITEM } from '../providers-model';

	interface Props {
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { onFinish, onCancel }: Props = $props();

	let key = $state('');
	let busy = $state(false);
	let error = $state('');

	async function submit() {
		if (!key.trim() || busy) return;
		busy = true;
		error = '';
		try {
			await providers.setFalKey(key);
			key = '';
			onFinish(FAL_KEY_ITEM);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not save the fal.ai key';
		} finally {
			busy = false;
		}
	}
</script>

<RowGroup title="Key">
	<SettingRow label="Media key" hint="Also reads the balance">
		<TextField bind:value={key} type="password" mono placeholder="fal.ai key" width={240} disabled={busy} />
	</SettingRow>
</RowGroup>
{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
<div class="pv-acts">
	<button type="button" class="st-btn ghost" disabled={busy} onclick={onCancel}>Cancel</button>
	<button type="button" class="st-btn primary" disabled={busy || !key.trim()} onclick={submit}>
		{busy ? 'Saving' : 'Use this key'}
	</button>
</div>
