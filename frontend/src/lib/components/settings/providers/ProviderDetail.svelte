<script lang="ts">
	/**
	 * A connected provider: its credentials (each with its own switch, or the
	 * Claude mode), the kinds it can still add, and removing everything.
	 */
	import { ChevronRight, Plus } from 'lucide-svelte';
	import { getModels } from '$lib/constants/models.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { notify } from '$lib/stores/notifications';
	import { getErrorMessage } from '$lib/api/client';
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ClaudeCliInstall from './setup/ClaudeCliInstall.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import {
		addableKinds,
		claudeGateNotice,
		claudeHeaderStatus,
		MODE_LABEL,
		MODE_OPTIONS,
		type CredentialView,
		type ProviderView
	} from './providers-model';
	import { changeClaudeMode, setCredentialEnabled, startSetup } from './provider-actions';

	interface Props {
		provider: ProviderView;
	}

	let { provider }: Props = $props();
	const access = getSettingsAccess();

	const claude = $derived(providers.current.claude);
	const mode = $derived<ClaudeAuthMode>(claude?.mode ?? 'subscription');
	const claudeNotice = $derived(claudeGateNotice(claude));
	const kinds = $derived(addableKinds(provider));
	const live = $derived(provider.credentials.filter((c) => c.on));
	const pickerModels = $derived(live.reduce((n, c) => n + (c.row?.models.length ?? 0), 0));

	const head = $derived.by(() => {
		if (provider.claude) return claudeHeaderStatus(claude);
		const total = provider.credentials.length;
		const status = live.length ? 'ok' : provider.credentials.some((c) => c.attention) ? 'warn' : 'off';
		return { status, text: `${live.length} of ${total} on` } as const;
	});

	const badge = $derived(provider.gateway ? 'Custom gateway' : provider.media ? 'Media provider' : 'Provider');
	const subtitle = $derived(
		provider.claude ? 'One credential per worker' : provider.media ? 'Used by media tools' : 'Each is a picker group'
	);

	const facts = $derived(
		provider.claude
			? [
					{ label: 'Runs on', value: MODE_LABEL[mode] },
					{ label: 'Credentials', value: provider.credentials.length },
					{ label: 'Models', value: `${getModels().length} built in` },
					{ label: 'Scope', value: 'This worker' }
				]
			: provider.media
				? [
						{ label: 'Credentials', value: provider.credentials.length },
						{ label: 'On', value: live.length },
						{ label: 'Picker groups', value: 'None' }
					]
				: [
						{ label: 'Credentials', value: provider.credentials.length },
						{ label: 'On', value: live.length },
						{ label: 'Models in picker', value: pickerModels },
						{ label: 'Picker groups', value: live.length }
					]
	);

	const credsCount = $derived(provider.claude ? (mode === 'off' ? 'none active' : 'one active') : `${live.length} on`);

	async function removeAll() {
		try {
			for (const c of provider.credentials) {
				if (c.draft) continue;
				if (c.flavor === 'fal') await providers.clearFalKey();
				else if (c.row) await providers.deleteProvider(c.row.id);
			}
		} catch (e) {
			notify(getErrorMessage(e, `Could not remove ${provider.name}`), { severity: 'error' });
		}
		if (provider.gateway) settingsNav.clearSelection('providers');
	}

	const danger = $derived<DangerAction[]>(
		provider.claude || provider.draft
			? []
			: [
					{
						label: `Remove ${provider.name}`,
						hint: 'Removes all its credentials',
						button: 'Remove provider',
						question: `Remove ${provider.credentials.length} credential${provider.credentials.length === 1 ? '' : 's'}?`,
						onConfirm: removeAll
					}
				]
	);

	function hintFor(c: CredentialView): string {
		const models = c.row && c.on && c.flavor !== 'fal' ? `, ${c.row.models.length} in picker` : '';
		return `${c.kind}${models}`;
	}
</script>

<DetailPane
	name={provider.name}
	status={head.status}
	statusText={head.text}
	{badge}
	{subtitle}
	{facts}
	{danger}
	item={provider.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet banner()}
		{#if provider.claude && claudeNotice}
			<InlineBanner tone="warn">{claudeNotice}</InlineBanner>
		{/if}
		{#if provider.claude && providers.current.claudeError}
			<InlineBanner tone="warn">{providers.current.claudeError}</InlineBanner>
		{/if}
	{/snippet}
	{#if provider.claude}
		<RowGroup title="Claude Code mode">
			<SettingRow id="providers.claudeMode">
				<Segmented
					options={MODE_OPTIONS}
					value={mode}
					disabled={!claude}
					onchange={(v: ClaudeAuthMode) => changeClaudeMode(v)}
				/>
			</SettingRow>
		</RowGroup>
		<ClaudeCliInstall manage isAdmin={!access.readOnly} />
	{/if}

	<RowGroup title="Credentials" count={credsCount} id="providers-credentials">
		{#each provider.credentials as c (c.item)}
			<SettingRow label={c.name} hint={hintFor(c)}>
				{#if c.switchable}
					<Switch checked={c.enabled} label="{c.name} on" onchange={(v) => setCredentialEnabled(c, v)} />
				{:else}
					<ValueText status={c.status} value={c.statusText} />
				{/if}
				<button
					type="button"
					class="st-btn icon ghost sm"
					aria-label="Open {c.name}"
					onclick={() => settingsNav.selectItem(c.item, 'providers')}
				>
					<ChevronRight />
				</button>
			</SettingRow>
		{/each}
	</RowGroup>

	{#if kinds.length && !provider.draft}
		<RowGroup title="Add">
			{#each kinds as k (k.menuLabel)}
				<SettingRow label={k.menuLabel} hint={k.hint}>
					<button type="button" class="st-btn" onclick={() => startSetup(provider, k.flavor)}><Plus />Add</button>
				</SettingRow>
			{/each}
		</RowGroup>
	{/if}

	{#if provider.media}
		<RowGroup title="Used by">
			<SettingRow label="Image, video, audio and 3D">
				<LinkValue label="Default models" target={{ category: 'models' }} />
			</SettingRow>
		</RowGroup>
	{/if}
</DetailPane>
