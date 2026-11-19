<script lang="ts">
	/**
	 * Anthropic API key > Models: what Anthropic's Models API lists for THIS key.
	 *
	 * The four aliases are always in the pickers. The versions the key lists
	 * are off until checked; checked versions appear in the pickers while the
	 * selected key lists them. Only the selected key's page can change that
	 * choice: an unselected key shows its own list read-only. Refresh asks
	 * Anthropic again (no prompt, no usage); a key never listed is asked once
	 * when the page opens. A chat that already picked a version keeps running
	 * it whatever this list says later.
	 */
	import { untrack } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import Checkbox from '$lib/components/ui/Checkbox.svelte';
	import { hydrateModelRegistry } from '$lib/constants/models.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import EmptyDetail from '../../shell/EmptyDetail.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';
	import TextField from '../../rows/TextField.svelte';

	interface ApiModel {
		value: string;
		label: string;
		created_at: string | null;
	}
	interface KeyModels {
		account_id: string;
		active: boolean;
		aliases: { value: string; label: string; runs: string | null }[];
		catalog: { fetched_at: string | null; models: ApiModel[] } | null;
		enabled: string[];
	}

	interface Props {
		/** The saved key's account id. */
		accountId: string;
		/** Whether this is the selected key (only its page chooses picker versions). */
		active: boolean;
		/** Viewer without settings access: the list shows, toggles and Refresh do not. */
		readOnly?: boolean;
	}

	let { accountId, active, readOnly = false }: Props = $props();

	let data = $state<KeyModels | null>(null);
	let loading = $state(false);
	let refreshing = $state(false);
	let busy = $state(false);
	let error = $state('');
	let filter = $state('');
	/** Optimistic checks while a save is in flight. */
	let pending = $state<Record<string, boolean>>({});
	/** The account the latest request was for: a reply for another key is dropped. */
	let current = '';

	const versions = $derived(data?.catalog?.models ?? []);
	const enabled = $derived(new Set(data?.enabled ?? []));
	const canChoose = $derived(!readOnly && active);
	const checkedOf = (value: string) => pending[value] ?? enabled.has(value);
	const shown = $derived.by(() => {
		const query = filter.trim().toLowerCase();
		return query
			? versions.filter((m) => m.value.toLowerCase().includes(query) || m.label.toLowerCase().includes(query))
			: versions;
	});

	$effect(() => {
		const id = accountId;
		untrack(() => void load(id));
	});

	async function load(id: string) {
		current = id;
		data = null;
		error = '';
		filter = '';
		loading = true;
		try {
			const result = await api.get<KeyModels>(`/models/claude/api-keys/${encodeURIComponent(id)}`);
			if (current !== id) return;
			data = result;
			// Never listed: ask once, so the page is useful without a click.
			if (!result.catalog && !readOnly) void refresh();
		} catch (e) {
			if (current === id) error = getErrorMessage(e, 'Could not load this key’s models.');
		} finally {
			if (current === id) loading = false;
		}
	}

	async function refresh() {
		const id = current;
		refreshing = true;
		error = '';
		try {
			const result = await api.post<KeyModels>(`/models/claude/api-keys/${encodeURIComponent(id)}/refresh`, {});
			if (current !== id) return;
			data = result;
			// The selected key's list decides which checked versions the pickers offer.
			if (result.active) await hydrateModelRegistry();
		} catch (e) {
			if (current === id) error = getErrorMessage(e, 'Could not ask Anthropic for this key’s models.');
		} finally {
			if (current === id) refreshing = false;
		}
	}

	async function toggle(value: string) {
		const id = current;
		const on = !checkedOf(value);
		pending = { ...pending, [value]: on };
		busy = true;
		error = '';
		try {
			// One id on or off: the other checked ids stay as they are.
			const saved = await api.put<{ models: string[] }>('/models/claude/api-enabled', { model: value, enabled: on });
			if (current === id && data) data = { ...data, enabled: saved.models };
			await hydrateModelRegistry();
			flashSaved();
		} catch (e) {
			if (current === id) error = getErrorMessage(e, 'Could not save the model list.');
		} finally {
			pending = {};
			busy = false;
		}
	}
</script>

<RowGroup title="Models" count={data?.catalog ? 'Anthropic API' : 'built in'} bare>
	{#snippet actions()}
		<button
			type="button"
			class="st-btn icon ghost sm"
			aria-label="Refresh model list"
			title="Refresh model list"
			disabled={loading || refreshing || busy || readOnly}
			onclick={refresh}
		>
			<RefreshCw />
		</button>
	{/snippet}
	<!-- One status line, so a refresh never moves the list below it. -->
	<p class="pv-msg" aria-live="polite" data-testid="claude-api-models-status">
		{#if refreshing}
			Asking Anthropic which models this key can use
		{:else if loading}
			Loading this key’s models
		{:else if data?.catalog?.fetched_at}
			Listed by Anthropic for this key <time datetime={data.catalog.fetched_at}>{new Date(data.catalog.fetched_at).toLocaleString()}</time>
		{:else}
			Anthropic has not listed this key’s models yet. Refresh asks it, without sending a prompt.
		{/if}
	</p>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="st-card pv-card">
		<div class="pv-mlist" data-testid="claude-api-alias-models">
			{#each data?.aliases ?? [] as m (m.value)}
				<div class="pv-mrow static">
					<StatusDot tone="ok" />
					<span class="id">{m.value}</span>
					<span class="lbl">{m.label}{m.runs ? ` · ${m.runs}` : ''}</span>
				</div>
			{/each}
		</div>
	</div>
	{#if data?.catalog}
		{#if versions.length === 0}
			<p class="pv-msg" data-testid="claude-api-models-empty">Anthropic listed no models for this key. The aliases above still work.</p>
		{:else}
			<p class="pv-msg">
				{#if canChoose}
					Versions this key can use. Checked ones appear in the model pickers while this key is selected.
				{:else if readOnly}
					Versions this key can use.
				{:else}
					Versions this key can use. Select this key to choose which ones the model pickers offer.
				{/if}
			</p>
			<div class="filter">
				<TextField bind:value={filter} type="search" placeholder="Filter models" label="Filter models" width={260} />
			</div>
			<div class="st-card pv-card">
				<div class="pv-mlist" data-testid="claude-api-versioned-models">
					{#each shown as m (m.value)}
						<label class="pv-mrow">
							<Checkbox checked={checkedOf(m.value)} disabled={busy || !canChoose} onchange={() => toggle(m.value)} />
							<span class="id">{m.value}</span>
							<span class="lbl">{m.label}</span>
						</label>
					{/each}
					{#if shown.length === 0}
						<EmptyDetail message={`No models match “${filter}”.`} actionLabel="Clear" onAction={() => (filter = '')} />
					{/if}
				</div>
			</div>
		{/if}
	{/if}
</RowGroup>

<style>
	.filter {
		margin: 8px 0;
	}
</style>
