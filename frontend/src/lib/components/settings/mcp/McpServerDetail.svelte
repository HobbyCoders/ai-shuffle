<script lang="ts">
	/**
	 * One MCP server, or the draft from "Add server". Edits stay in the form
	 * until Save (or "Add server" for the draft); Discard goes back to the
	 * saved values. Built-in servers are read-only.
	 */
	import { getScopeLabel, getServerTypeLabel } from '$lib/api/mcp';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import PairRows from './PairRows.svelte';
	import { DRAFT_ID, TYPE_OPTIONS, detailLine, typeIcon, type McpModel } from './mcp-model.svelte';

	interface Props {
		model: McpModel;
		/** A server name, or DRAFT_ID. */
		id: string;
	}

	let { model, id }: Props = $props();

	const CATEGORY = 'mcp' as const;
	// Forms are created outside rendering; the collection keys this component by id.
	// svelte-ignore state_referenced_locally
	model.ensure(id);

	const isDraft = $derived(id === DRAFT_ID);
	const server = $derived(isDraft ? undefined : model.server(id));
	const form = $derived(model.form(id));
	const dirty = $derived(model.dirty(id));
	const saving = $derived(model.saving === id);
	const errors = $derived(model.errors[id] ?? []);
	const remote = $derived(model.remote[id]);
	const remoteTransport = $derived(form ? form.type !== 'stdio' : false);

	async function save() {
		const name = await model.save(id);
		if (!name) return;
		flashSaved();
		if (isDraft) settingsNav.selectItem(name, CATEGORY);
	}

	function cancelDraft() {
		model.discardDraft();
		settingsNav.clearSelection(CATEGORY);
	}

	async function remove() {
		if (await model.remove(id)) settingsNav.clearSelection(CATEGORY);
	}
</script>

{#snippet notices()}
	{#if remote === 'changed'}
		<InlineBanner tone="warn">
			Changed elsewhere, your edits kept
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => model.loadLatest(id)}>Load latest</button>
			{/snippet}
		</InlineBanner>
	{:else if remote === 'deleted'}
		<InlineBanner tone="warn">Deleted elsewhere. Save adds it back.</InlineBanner>
	{/if}
	{#if errors.length}
		<InlineBanner tone="warn">
			<span role="alert">{errors.join('. ')}</span>
		</InlineBanner>
	{/if}
{/snippet}

{#if server?.builtin}
	<DetailPane
		name={server.name}
		icon={typeIcon(server.type)}
		status={server.enabled ? 'ok' : 'off'}
		statusText={server.enabled ? 'Enabled' : 'Disabled'}
		badge="Built-in"
		subtitle={server.description || undefined}
		category={CATEGORY}
		item={server.name}
		facts={[
			{ label: 'Type', value: getServerTypeLabel(server.type) },
			{ label: 'Scope', value: 'Built-in' }
		]}
	>
		{#snippet children()}
			<RowGroup title="Server">
				<SettingRow label="Runs as">
					<ValueText value={detailLine(server)} mono />
				</SettingRow>
				<SettingRow label="Editing" hint="Ships with AI Shuffle">
					<ValueText value="Read-only" />
				</SettingRow>
			</RowGroup>
			<p class="st-note">Enable per project in Projects.</p>
		{/snippet}
	</DetailPane>
{:else if form}
	<DetailPane
		name={isDraft ? form.name.trim() || 'New server' : id}
		icon={typeIcon(form.type)}
		status={dirty ? 'warn' : isDraft ? 'busy' : server?.enabled ? 'ok' : 'off'}
		statusText={isDraft ? 'Not added yet' : dirty ? 'Unsaved changes' : server?.enabled ? 'Enabled' : 'Disabled'}
		badge="MCP server"
		subtitle={!isDraft ? server?.description || undefined : undefined}
		category={CATEGORY}
		item={id}
		facts={isDraft
			? []
			: [
					{ label: 'Type', value: server ? getServerTypeLabel(server.type) : null },
					{ label: 'Scope', value: server ? getScopeLabel(server.scope) : null },
					{ label: 'Variables', value: Object.keys(server?.env ?? {}).length },
					...(server && server.type !== 'stdio'
						? [{ label: 'Headers', value: Object.keys(server.headers ?? {}).length }]
						: [])
				]}
		danger={isDraft || !server
			? []
			: [
					{
						label: 'Delete this server',
						hint: 'Removed from every project',
						button: 'Delete',
						question: `Delete ${id}?`,
						onConfirm: remove
					}
				]}
	>
		{#snippet primary()}
			{#if isDraft}
				<button type="button" class="st-btn ghost" onclick={cancelDraft}>Cancel</button>
				<button type="button" class="st-btn primary" disabled={saving} onclick={save}>
					{saving ? 'Adding…' : 'Add server'}
				</button>
			{:else}
				{#if dirty}
					<button type="button" class="st-btn ghost" disabled={saving} onclick={() => model.discard(id)}>Discard</button>
				{/if}
				<button type="button" class="st-btn primary" disabled={!dirty || saving} onclick={save}>
					{saving ? 'Saving…' : 'Save'}
				</button>
			{/if}
		{/snippet}
		{#snippet banner()}
			{@render notices()}
		{/snippet}
		{#snippet children()}
			<RowGroup title="Server">
				<SettingRow label="Name" hint={isDraft ? 'Letters, numbers, dashes, underscores' : 'Fixed after adding'}>
					{#if isDraft}
						<TextField bind:value={form.name} placeholder="github" mono maxlength={100} />
					{:else}
						<ValueText value={id} mono tone="fg" />
					{/if}
				</SettingRow>
				<SettingRow label="Description">
					<TextField bind:value={form.description} placeholder="Optional" width={280} />
				</SettingRow>
			</RowGroup>
			<RowGroup title="Transport">
				<SettingRow label="Type">
					<Segmented options={TYPE_OPTIONS} bind:value={form.type} size="sm" />
				</SettingRow>
				{#if form.type === 'stdio'}
					<SettingRow label="Command" hint="Program to start">
						<TextField bind:value={form.command} placeholder="npx" mono width={300} />
					</SettingRow>
					<SettingRow label="Arguments" hint="Separated by spaces">
						<TextField bind:value={form.args} placeholder="-y @modelcontextprotocol/server-github" mono width={300} />
					</SettingRow>
				{:else}
					<SettingRow label="URL">
						<TextField bind:value={form.url} type="url" placeholder="https://mcp.example.com/mcp" mono width={300} />
					</SettingRow>
				{/if}
			</RowGroup>
			{#if remoteTransport}
				<PairRows
					title="Headers"
					bind:pairs={form.headers}
					addLabel="Add header"
					keyPlaceholder="Authorization"
					empty="No headers."
				/>
			{/if}
			<PairRows
				title="Environment"
				bind:pairs={form.env}
				addLabel="Add variable"
				keyPlaceholder="GITHUB_TOKEN"
				empty="No variables."
			/>
			<p class="st-note">Enable per project in Projects.</p>
		{/snippet}
	</DetailPane>
{/if}
