<script lang="ts">
	/**
	 * Claude subscription > Models: every model this worker's Claude Code CLI
	 * offers, all of them in the model pickers. The four aliases come first,
	 * labelled as the CLI names them today ("Opus 5.5"), then the versions it
	 * lists by id ("Opus 4.6", Mythos). Refresh asks the CLI again without
	 * sending a prompt. Before the CLI has reported a list, this shows the
	 * built-in aliases.
	 */
	import { RefreshCw } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { getClaudeAuth, getModels, hydrateModelRegistry } from '$lib/constants/models.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import AvailableModelList from './AvailableModelList.svelte';

	interface Props {
		/** Viewer without settings access: the list shows, Refresh does not run. */
		readOnly?: boolean;
	}

	let { readOnly = false }: Props = $props();

	const models = $derived(getModels().map(({ value, label }) => ({ id: value, label })));
	const captured = $derived(!!getClaudeAuth().cli_models);
	let refreshing = $state(false);
	let error = $state('');

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

<RowGroup title="Models" count={models.length} bare>
	{#snippet actions()}
		<button
			type="button"
			class="st-btn icon ghost sm"
			aria-label="Refresh model list"
			title="Refresh model list"
			disabled={refreshing || readOnly}
			onclick={refresh}
		>
			<RefreshCw />
		</button>
	{/snippet}
	{#if captured}
		<p class="pv-msg">Models Claude Code offers on this worker. All of them appear in the model pickers.</p>
	{:else}
		<p class="pv-msg">Claude Code has not listed its models on this worker yet. Refresh asks it, without sending a prompt.</p>
	{/if}
	{#if refreshing}<p class="pv-msg" aria-live="polite">Asking Claude Code for its models</p>{/if}
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div data-testid="claude-cli-models">
		<AvailableModelList {models} emptyMessage="No models reported yet." />
	</div>
</RowGroup>
