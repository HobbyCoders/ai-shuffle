<script lang="ts">
	/**
	 * The fal.ai media key: image, video, audio and 3D tools run on it, and it
	 * also reads the account balance. Its switch turns fal.ai off without
	 * forgetting the key.
	 */
	import { onMount } from 'svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { formatAsOf } from '$lib/stores/rate-limit';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import TextField from '../rows/TextField.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import Switch from '../rows/Switch.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import { FAL_KEY_ITEM, type CredentialView, type ProviderView } from './providers-model';
	import { setCredentialEnabled } from './provider-actions';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const s = $derived(providers.current);
	const entry = $derived(s.usage?.providers?.find((p) => p.provider_id === 'fal-ai') ?? null);
	const balanceValue = $derived.by(() => {
		const raw = entry?.credits?.balance_remaining;
		return typeof raw === 'number' && Number.isFinite(raw) ? `$${raw.toFixed(2)}` : null;
	});
	const checked = $derived(entry?.fetched_at ? formatAsOf(entry.fetched_at, Date.now()) : null);

	let key = $state('');
	let saving = $state(false);
	let message = $state('');
	let error = $state('');

	async function save() {
		if (!key.trim() || saving) return;
		saving = true;
		error = '';
		message = '';
		try {
			const res = await providers.setFalKey(key);
			key = '';
			message = res.valid ? 'Saved and verified' : 'Saved, not verified';
			flashSaved();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	const danger = $derived<DangerAction[]>([
		{
			label: 'Remove media key',
			hint: 'Media generation stops',
			button: 'Remove',
			question: 'Remove the media key?',
			onConfirm: async () => {
				try {
					await providers.clearFalKey();
					settingsNav.selectItem(provider.item, 'providers');
				} catch (e) {
					error = e instanceof Error ? e.message : 'Remove failed';
				}
			}
		}
	]);

	onMount(() => void providers.loadUsage());
</script>

<DetailPane
	name={credential.name}
	badge="API key"
	status={credential.status}
	statusText={credential.statusText}
	subtitle={s.falEnabled ? 'Used by media tools' : 'Media tools are off'}
	facts={[
		{ label: 'Key', value: s.falKeyMasked || 'Stored', mono: true },
		{ label: 'Balance', value: balanceValue },
		{ label: 'Checked', value: checked }
	]}
	{danger}
	item={FAL_KEY_ITEM}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" status={credential.status} />{/snippet}
	{#snippet primary()}
		<span class="onoff">
			{s.falEnabled ? 'On' : 'Off'}
			<Switch checked={s.falEnabled} label="fal.ai on" onchange={(v) => setCredentialEnabled(credential, v)} />
		</span>
	{/snippet}
	<RowGroup title="Key">
		<SettingRow label="Media key">
			<ValueText mono tone="fg" value={s.falKeyMasked || 'Stored'} />
		</SettingRow>
		<SettingRow id="providers.falKey">
			<TextField bind:value={key} type="password" mono placeholder="New fal.ai key" width={180} disabled={saving} label="New fal.ai key" />
			<button type="button" class="st-btn" disabled={saving || !key.trim()} onclick={save}>Save</button>
		</SettingRow>
	</RowGroup>
	{#if message}<p class="pv-msg ok" role="status">{message}</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<RowGroup title="Balance">
		<SettingRow id="providers.falBalance">
			<ValueText tone="fg" value={balanceValue ?? 'Not reported yet'} />
		</SettingRow>
	</RowGroup>
	<RowGroup title="Used by">
		<SettingRow label="Image, video, audio and 3D">
			<LinkValue label="Default models" target={{ category: 'models' }} />
		</SettingRow>
	</RowGroup>
</DetailPane>

<style>
	.onoff {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 13px;
		color: var(--st-mfg);
	}
</style>
