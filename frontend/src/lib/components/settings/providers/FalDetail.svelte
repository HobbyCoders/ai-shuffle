<script lang="ts">
	import { balanceDisplay, safeKeyHint } from './credential-display';
	import DetailLine from './DetailLine.svelte';
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
	const balance = $derived(balanceDisplay(entry?.credits?.balance_remaining));
	const checked = $derived(entry?.fetched_at ? formatAsOf(entry.fetched_at, Date.now()) : null);

	let toggleError = $state('');
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
			// A failure throws through to the Danger zone, which shows it by Remove.
			onConfirm: async () => {
				const back = provider.item;
				await providers.clearFalKey();
				settingsNav.selectItem(back, 'providers');
			}
		}
	]);

	onMount(() => void providers.loadUsage());
</script>

{#snippet subtitle()}
	<DetailLine parts={[s.falEnabled ? 'Used by media tools' : 'Media tools are off', balance, checked]} />
{/snippet}

<DetailPane
	name={credential.name}
	{subtitle}
	{danger}
	item={FAL_KEY_ITEM}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		<span class="onoff-wrap">
		<span class="onoff">
			<span class="onoff-state">{s.falEnabled ? 'On' : 'Off'}</span>
			<Switch checked={s.falEnabled} label="fal.ai on" onchange={async (v) => { toggleError = (await setCredentialEnabled(credential, v)) ?? ''; }} />
		</span>
		{#if toggleError}<span class="onoff-err" role="alert">{toggleError}</span>{/if}
		</span>
	{/snippet}
	<RowGroup title="Key">
		<SettingRow label="Media key">
			<ValueText mono tone="fg" value={safeKeyHint(s.falKeyMasked) || 'Saved'} />
		</SettingRow>
		<SettingRow id="providers.falKey">
			<TextField
				bind:value={key}
				type="password"
				mono
				placeholder="New fal.ai key"
				width={260}
				disabled={saving}
				label="New fal.ai key"
			/>
			<button type="button" class="st-btn" disabled={saving || !key.trim()} onclick={save}>Save</button>
		</SettingRow>
	</RowGroup>
	{#if message}<p class="pv-msg ok" role="status">{message}</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<RowGroup title="Balance">
		<SettingRow id="providers.falBalance">
			<ValueText tone={balance?.danger ? 'danger' : 'fg'} value={balance?.text ?? 'Not reported yet'} />
		</SettingRow>
	</RowGroup>
	<RowGroup title="Used by">
		<SettingRow label="Image, video, audio and 3D">
			<LinkValue label="Default models" target={{ category: 'models' }} />
		</SettingRow>
	</RowGroup>
</DetailPane>

<style>
	.onoff-state { font-size: 12px; color: var(--st-mfg); white-space: nowrap; }
	.onoff-wrap { display: inline-flex; flex-direction: column; align-items: flex-end; gap: 4px; max-width: 100%; }
	.onoff {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 13px;
		color: var(--st-mfg);
	}
	/* Capped: on phone the switch sits in the More menu, which sizes to its content. */
	.onoff-err { max-width: min(18rem, 70vw); text-align: right; font-size: 12px; color: var(--st-destructive); overflow-wrap: anywhere; }
</style>
