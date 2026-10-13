<script lang="ts">
	/**
	 * SourcesDetail: where agent plugins come from (git marketplaces: add,
	 * sync, remove), updates for installed plugins (check, update one or all)
	 * and the auto-update schedule. Owner only.
	 */
	import { ArrowUpCircle, Plus, RefreshCw } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import { getPluginsModel, plural } from './plugins-model.svelte';
	import { sourceNameFromUrl } from './plugin-parts';
	import { SOURCES_ICON } from './icons';

	const INTERVALS = [1, 3, 6, 12, 24].map((h) => ({ value: String(h), label: plural(h, 'hour') }));

	const model = getPluginsModel();
	const auto = $derived(model.autoUpdate);
	let adding = $state(false);
	let sourceName = $state('');
	let sourceUrl = $state('');
	/** The owner typed a name; stop following the URL. */
	let nameEdited = $state(false);

	function onUrl(value: string) {
		sourceUrl = value;
		if (!nameEdited) sourceName = sourceNameFromUrl(value);
	}

	function onName(value: string) {
		sourceName = value;
		nameEdited = value.trim() !== '';
	}

	function closeForm() {
		adding = false;
		sourceName = '';
		sourceUrl = '';
		nameEdited = false;
	}

	function synced(value: string | null): string {
		if (!value) return 'Never synced';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return 'Unknown';
		return `Synced ${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
	}

	function ago(value: string | null | undefined): string {
		if (!value) return 'Never';
		const mins = Math.floor((Date.now() - new Date(value).getTime()) / 60000);
		if (Number.isNaN(mins)) return 'Unknown';
		if (mins < 1) return 'Just now';
		if (mins < 60) return `${mins}m ago`;
		const hours = Math.floor(mins / 60);
		return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
	}

	async function add() {
		if (await model.addSource(sourceUrl, sourceName)) closeForm();
	}

	const facts = $derived<Fact[]>([
		{ label: 'Sources', value: model.marketplaces.length },
		{ label: 'Updates', value: model.updates.length },
		{ label: 'Auto-update', value: auto ? (auto.enabled ? 'On' : 'Off') : null },
		{ label: 'Last check', value: auto ? ago(auto.last_check) : null }
	]);
</script>

{#snippet addForm()}
	<div class="add">
		<TextField
			label="Git URL"
			placeholder="https://github.com/user/plugins.git"
			value={sourceUrl}
			oninput={onUrl}
			mono
			width={320}
		/>
		<TextField label="Name" placeholder="From the URL" value={sourceName} oninput={onName} width={180} />
		<span class="acts">
			<button type="button" class="st-btn ghost sm" onclick={closeForm}>Cancel</button>
			<button
				type="button"
				class="st-btn primary sm"
				disabled={!sourceUrl.trim() || model.addingSource}
				onclick={add}
			>
				{model.addingSource ? 'Adding…' : 'Add'}
			</button>
		</span>
	</div>
{/snippet}

{#snippet banner()}
	{#each model.marketplaces.filter((source) => source.error) as source (source.id)}
		<InlineBanner tone="warn"><span role="alert">{source.error}</span></InlineBanner>
	{/each}
	{#if model.actionError}
		<InlineBanner tone="warn"><span role="alert">{model.actionError}</span></InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="Sources and updates"
	icon={SOURCES_ICON}
	subtitle="Where plugins come from"
	{facts}
	{banner}
	category="plugins"
	item="sources"
>
	<RowGroup title="Updates" count={model.updates.length || undefined}>
		<SettingRow id="plugins.updates" hint={model.updates.length ? `${model.updates.length} available` : 'Up to date'}>
			<button type="button" class="st-btn sm" disabled={model.checking} onclick={() => model.checkUpdates()}>
				<RefreshCw />{model.checking ? 'Checking…' : 'Check'}
			</button>
			{#if model.updates.length}
				<button type="button" class="st-btn primary sm" disabled={model.updatingAll} onclick={() => model.updateAll()}>
					<ArrowUpCircle />{model.updatingAll ? 'Updating…' : `Update all (${model.updates.length})`}
				</button>
			{/if}
		</SettingRow>
		{#each model.updates as u (u.plugin_id)}
			<SettingRow label={u.name} hint="v{u.installed_version} to v{u.available_version}">
				<button
					type="button"
					class="st-btn sm"
					aria-label="Update {u.name}"
					disabled={!!model.updating[u.plugin_id] || model.updatingAll}
					onclick={() => model.update(u.plugin_id)}
				>
					{model.updating[u.plugin_id] ? 'Updating…' : 'Update'}
				</button>
			</SettingRow>
		{/each}
	</RowGroup>

	<RowGroup title="Auto-update">
		<SettingRow id="plugins.autoUpdate">
			<Switch checked={!!auto?.enabled} disabled={!auto} onchange={(on) => model.setAutoUpdate({ enabled: on })} />
		</SettingRow>
		<SettingRow id="plugins.updateInterval" dim={!auto?.enabled}>
			<SelectField
				value={String(auto?.interval_hours ?? 24)}
				options={INTERVALS}
				disabled={!auto?.enabled}
				width={140}
				onchange={(v) => model.setAutoUpdate({ interval_hours: Number(v) })}
			/>
		</SettingRow>
		<SettingRow id="plugins.autoInstall" dim={!auto?.enabled}>
			<Switch
				checked={!!auto?.auto_update_plugins}
				disabled={!auto?.enabled}
				onchange={(on) => model.setAutoUpdate({ auto_update_plugins: on })}
			/>
		</SettingRow>
		<SettingRow label="Last checked" hint={auto ? ago(auto.last_check) : 'Never'}>
			<button type="button" class="st-btn sm" disabled={model.checking} onclick={() => model.runAutoUpdate()}>
				<RefreshCw />Check now
			</button>
		</SettingRow>
	</RowGroup>

	<div data-setting-anchor="plugins.sources">
		<RowGroup title="Sources" count={model.marketplaces.length}>
			{#each model.marketplaces as m (m.id)}
				<SettingRow label={m.display_name ?? m.id} hint={m.url} hintMono>
					{#if m.skipped?.length}
						<button
							type="button"
							class="st-btn ghost sm skip"
							title="Some plugins can't install here"
							onclick={() => settingsNav.selectItem('explore', 'plugins')}
						>
							{m.skipped.length} skipped
						</button>
					{/if}
					<ValueText value={synced(m.last_updated)} />
					<button
						type="button"
						class="st-btn icon sm"
						aria-label="Sync {m.id}"
						title="Sync from git"
						disabled={!!model.syncing[m.id]}
						onclick={() => model.syncSource(m.id)}
					>
						<RefreshCw class={model.syncing[m.id] ? 'spin' : ''} />
					</button>
					<InlineConfirm
						label="Remove"
						question="Remove {m.id}?"
						variant="ghost"
						size="sm"
						onConfirm={() => model.removeSource(m.id)}
					/>
				</SettingRow>
			{:else}
				<SettingRow label="No sources yet" />
			{/each}
			<SettingRow label="Add source" hint="A git repository URL" expanded={adding ? addForm : undefined}>
				{#if !adding}
					<button type="button" class="st-btn sm" onclick={() => (adding = true)}><Plus />Add source</button>
				{/if}
			</SettingRow>
		</RowGroup>
	</div>
</DetailPane>

<style>
	.add {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
	}
	.acts {
		display: flex;
		gap: 6px;
		margin-left: auto;
	}
	.skip {
		color: var(--st-warning);
	}
</style>
