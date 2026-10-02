<script lang="ts">
	/**
	 * ConnectionDetail: one connected app (a connector type). Its official
	 * runtime ships with AI Shuffle: install it, turn it on or off, update,
	 * roll back or uninstall it, and manage its accounts (ConnectorManager,
	 * which carries the Google and Plaid sign-in flows). Disabling or removing
	 * a runtime warns first when connections or automations still use it.
	 */
	import { untrack } from 'svelte';
	import { ArrowUpCircle, Download } from 'lucide-svelte';
	import ConnectorManager from '$lib/components/ConnectorManager.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import { connectionItem, getPluginsModel, plural } from './plugins-model.svelte';
	import { connectionIcon } from './icons';

	interface Props {
		/** Connector type, e.g. `gmail`. */
		type: string;
	}

	let { type }: Props = $props();
	const model = getPluginsModel();
	const item = $derived(connectionItem(type));
	const entry = $derived(model.entryByItem(item));
	const info = $derived(entry?.type);
	const refs = $derived(model.refs[type] ?? []);
	const automations = $derived(refs.reduce((n, r) => n + r.automations.length, 0));
	const busy = $derived(model.busy === type);
	const installed = $derived(!!entry?.installed);
	const enabled = $derived(!!entry?.enabled);
	const count = $derived(entry?.count ?? 0);
	const name = $derived(entry?.name ?? type);

	$effect(() => {
		void type;
		untrack(() => model.loadReferences(type));
	});

	function refreshAfterAccounts() {
		void model.load();
		void model.loadReferences(type);
	}

	const usedBy = $derived(
		refs.length
			? `${plural(refs.length, 'connection')}${automations ? `, ${plural(automations, 'automation')}` : ''}`
			: null
	);

	const facts = $derived<Fact[]>([
		{ label: 'Version', value: installed && info?.plugin_version ? `v${info.plugin_version}` : null, mono: true },
		{ label: 'Accounts', value: count },
		{ label: 'Used by', value: usedBy },
		{ label: 'Source', value: 'Official' }
	]);

	const status = $derived(entry?.failing ? 'err' : installed ? (enabled ? 'ok' : 'off') : count ? 'ok' : undefined);
	const statusText = $derived(
		entry?.failing
			? 'Account error'
			: installed
				? enabled
					? 'Runtime on'
					: 'Runtime off'
				: count
					? 'Bundled runtime'
					: 'Not installed'
	);

	const tabs = $derived(installed ? ['Accounts', 'Runtime'] : []);

	const danger = $derived<DangerAction[]>(
		installed
			? [
					{
						label: 'Uninstall runtime',
						hint: usedBy ? `Used by ${usedBy}` : 'Remove accounts first',
						button: 'Uninstall',
						question: usedBy ? `Uninstall? ${usedBy} use it.` : `Uninstall the ${name} runtime?`,
						onConfirm: async () => {
							await model.changeRuntime(type, 'uninstall');
						},
						disabled: busy
					}
				]
			: []
	);
</script>

{#snippet primary()}
	{#if !installed}
		<button type="button" class="st-btn primary" disabled={busy} onclick={() => model.changeRuntime(type, 'install')}>
			<Download />{busy ? 'Installing…' : `Install ${name}`}
		</button>
	{:else if refs.length && enabled}
		<!-- Turning off a runtime that connections use asks first. -->
		<InlineConfirm
			question="Disable? {usedBy} use it."
			confirmLabel="Disable"
			onConfirm={async () => {
				await model.changeRuntime(type, 'disable');
			}}
		>
			{#snippet trigger(arm)}
				<Switch
					checked={enabled}
					label="Runtime"
					disabled={busy}
					onchange={(on) => {
						// The switch stays on until the confirm lands.
						if (!on) arm();
						return Promise.resolve(false);
					}}
				/>
			{/snippet}
		</InlineConfirm>
	{:else}
		<Switch
			checked={enabled}
			label="Runtime"
			disabled={busy}
			onchange={(on) => model.changeRuntime(type, on ? 'enable' : 'disable')}
		/>
	{/if}
{/snippet}

{#snippet banner()}
	{#if model.actionError}
		<InlineBanner tone="warn"><span role="alert">{model.actionError}</span></InlineBanner>
	{/if}
	{#if busy && !installed}
		<InlineBanner tone="info">Installing runtime and dependencies…</InlineBanner>
	{:else if installed && info?.plugin_update_available}
		<InlineBanner tone="info" icon={ArrowUpCircle}>
			Runtime update ready
			{#snippet actions()}
				<button type="button" class="st-btn sm" disabled={busy} onclick={() => model.changeRuntime(type, 'install')}>
					Update runtime
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

{#snippet setupRows()}
	<RowGroup title="Runtime">
		<SettingRow label="Source"><ValueText value="Bundled with AI Shuffle" /></SettingRow>
		<SettingRow label="GitHub access"><ValueText value="Not needed" /></SettingRow>
		<SettingRow label="Dependencies"><ValueText value="Downloads from PyPI" /></SettingRow>
		<SettingRow label="Runs as"><ValueText value="Its own process" /></SettingRow>
		{#if count}
			<SettingRow label="Existing accounts"><ValueText value="Keep their IDs" /></SettingRow>
		{/if}
	</RowGroup>
{/snippet}

{#snippet refRows()}
	<RowGroup title="Used by" count={refs.length}>
		{#each refs as ref (ref.id)}
			<SettingRow label={ref.name}>
				<ValueText value={ref.active ? 'Active' : 'Inactive'} status={ref.active ? 'ok' : 'off'} />
			</SettingRow>
			{#each ref.automations as automation (automation.id)}
				<SettingRow label="↳ {automation.name}" hint="Automation">
					<ValueText value={automation.is_enabled ? 'On' : 'Paused'} />
				</SettingRow>
			{/each}
		{/each}
	</RowGroup>
{/snippet}

<DetailPane
	{name}
	icon={connectionIcon(type)}
	{status}
	{statusText}
	badge="App"
	subtitle={entry?.description}
	{primary}
	{facts}
	{banner}
	{tabs}
	category="plugins"
	{item}
	{danger}
>
	{#snippet children(tab)}
		{#if !installed}
			{@render setupRows()}
			{#if count}
				<RowGroup title="Accounts" {count} bare>
					{#key `${type}:${installed}`}
						<div class="cm"><ConnectorManager typeFilter={type} onChanged={refreshAfterAccounts} /></div>
					{/key}
				</RowGroup>
			{/if}
			{#if refs.length}{@render refRows()}{/if}
		{:else if tab === 'Runtime'}
			<RowGroup title="Runtime">
				<SettingRow label="Status">
					<ValueText value={enabled ? 'On' : 'Off'} status={enabled ? 'ok' : 'off'} />
				</SettingRow>
				<SettingRow label="Version"><ValueText value={info?.plugin_version ?? 'Unknown'} mono /></SettingRow>
				{#if info?.plugin_update_available}
					<SettingRow label="Update" hint="Pause automations first">
						<button
							type="button"
							class="st-btn sm"
							disabled={busy}
							onclick={() => model.changeRuntime(type, 'install')}
						>
							Update runtime
						</button>
					</SettingRow>
				{/if}
				{#if info?.plugin_rollback_available}
					<SettingRow label="Roll back" hint="Accounts and settings kept">
						<InlineConfirm
							label="Roll back runtime"
							question="Roll back to the last version?"
							confirmLabel="Roll back"
							variant="default"
							tone="primary"
							size="sm"
							disabled={busy}
							onConfirm={async () => {
								await model.changeRuntime(type, 'rollback');
							}}
						/>
					</SettingRow>
				{/if}
				<SettingRow label="Source"><ValueText value="Bundled with AI Shuffle" /></SettingRow>
			</RowGroup>
			{#if refs.length}{@render refRows()}{/if}
		{:else if enabled || count}
			{#key `${type}:${installed}`}
				<div class="cm"><ConnectorManager typeFilter={type} onChanged={refreshAfterAccounts} /></div>
			{/key}
		{:else}
			<p class="st-empty">Turn the runtime on to add accounts.</p>
		{/if}
	{/snippet}
</DetailPane>

<style>
	.cm {
		margin-top: 18px;
		min-width: 0;
	}
</style>
