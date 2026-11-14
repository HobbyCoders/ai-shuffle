<script lang="ts">
	/**
	 * Claude subscription > Models: what this worker's Claude Code CLI offers.
	 *
	 * The four aliases are always in the pickers, labelled as the CLI names
	 * them today ("Opus 5.5"). The versions the CLI lists by id ("Opus 4.6")
	 * are off until checked here; each toggle saves on the spot. Refresh asks
	 * the CLI again without sending a prompt. Before the CLI has reported a
	 * list, this shows the built-in aliases.
	 */
	import { RefreshCw } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import Checkbox from '$lib/components/ui/Checkbox.svelte';
	import {
		getClaudeAuth,
		getClaudeVersionedModels,
		getModels,
		hydrateModelRegistry
	} from '$lib/constants/models.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import EmptyDetail from '../../shell/EmptyDetail.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';
	import TextField from '../../rows/TextField.svelte';

	interface Props {
		/** Viewer without settings access: the list shows, toggles do not. */
		readOnly?: boolean;
	}

	let { readOnly = false }: Props = $props();

	const aliases = $derived(getModels().filter((m) => !m.value.startsWith('claude-')));
	const versions = $derived(getClaudeVersionedModels());
	const captured = $derived(getClaudeAuth().cli_models ?? null);
	/** Optimistic checks while a save is in flight. */
	let pending = $state<Record<string, boolean>>({});
	let busy = $state(false);
	let refreshing = $state(false);
	let error = $state('');
	let filter = $state('');

	const checkedOf = (value: string, enabled: boolean) => pending[value] ?? enabled;
	const shown = $derived.by(() => {
		const query = filter.trim().toLowerCase();
		return query
			? versions.filter((m) => m.value.toLowerCase().includes(query) || m.label.toLowerCase().includes(query))
			: versions;
	});

	async function toggle(value: string) {
		const model = versions.find((m) => m.value === value);
		const enabled = !checkedOf(value, model?.enabled ?? false);
		pending = { ...pending, [value]: enabled };
		busy = true;
		error = '';
		try {
			// One id on or off: ids this worker's CLI does not list stay enabled.
			await api.put('/models/claude/enabled', { model: value, enabled });
			await hydrateModelRegistry();
			flashSaved();
		} catch (e) {
			error = getErrorMessage(e, 'Could not save the model list.');
		} finally {
			pending = {};
			busy = false;
		}
	}

	async function refresh() {
		refreshing = true;
		error = '';
		try {
			await api.post('/models/claude/refresh', {});
			await hydrateModelRegistry();
		} catch (e) {
			error = getErrorMessage(e, 'Could not ask Claude Code for its models.');
		} finally {
			refreshing = false;
		}
	}
</script>

<RowGroup title="Models" count={captured ? `Claude Code ${captured.cli_version ?? ''}`.trim() : 'built in'} bare>
	{#snippet actions()}
		<button
			type="button"
			class="st-btn icon ghost sm"
			aria-label="Refresh model list"
			title="Refresh model list"
			disabled={refreshing || busy || readOnly}
			onclick={refresh}
		>
			<RefreshCw />
		</button>
	{/snippet}
	{#if captured?.captured_at}
		<p class="pv-msg">Listed by Claude Code <time datetime={captured.captured_at}>{new Date(captured.captured_at).toLocaleString()}</time></p>
	{:else}
		<p class="pv-msg">Claude Code has not listed its models on this worker yet. Refresh asks it, without sending a prompt.</p>
	{/if}
	{#if refreshing}<p class="pv-msg" aria-live="polite">Asking Claude Code for its models</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="st-card pv-card">
		<div class="pv-mlist" data-testid="claude-alias-models">
			{#each aliases as m (m.value)}
				<div class="pv-mrow static">
					<StatusDot tone="ok" />
					<span class="id">{m.value}</span>
					<span class="lbl">{m.label}</span>
				</div>
			{/each}
		</div>
	</div>
	{#if versions.length > 0}
		<p class="pv-msg">Other versions this Claude Code offers. Checked ones appear in the model pickers.</p>
		<div class="filter">
			<TextField bind:value={filter} type="search" placeholder="Filter models" label="Filter models" width={260} />
		</div>
		<div class="st-card pv-card">
			<div class="pv-mlist" data-testid="claude-versioned-models">
				{#each shown as m (m.value)}
					<label class="pv-mrow">
						<Checkbox checked={checkedOf(m.value, m.enabled)} disabled={busy || readOnly} onchange={() => toggle(m.value)} />
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
</RowGroup>

<style>
	.filter {
		margin: 8px 0;
	}
</style>
