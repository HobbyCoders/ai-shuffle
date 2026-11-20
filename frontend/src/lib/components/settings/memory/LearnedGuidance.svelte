<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage, isApiError } from '$lib/api/client';
	import DetailPane from '../shell/DetailPane.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import GuidanceEditor, { type Block } from './GuidanceEditor.svelte';
	interface SavedBlocks { blocks: Block[]; max_chars: number }

	let data = $state<SavedBlocks>();
	let loading = $state(true);
	let error = $state('');
	const path = '/learned-guidance';

	async function load() {
		loading = true;
		error = '';
		try {
			data = await api.get<SavedBlocks>(path);
		} catch (e) {
			error = isApiError(e) && e.status === 403
				? 'Sign in as the owner to manage learned guidance.'
				: getErrorMessage(e, 'Could not load learned guidance.');
		} finally {
			loading = false;
		}
	}

	async function save(block: Block, content: string) {
		return api.put<Block>(`${path}/${block.id}`, { content, expected_revision: block.revision });
	}

	async function reload(id: string) {
		return (await api.get<SavedBlocks>(path)).blocks.find((b) => b.id === id);
	}

	function remove(id: string) {
		if (data) data.blocks = data.blocks.filter((b) => b.id !== id);
	}

	onMount(() => void load());
</script>

<DetailPane name="Learned guidance" category="memory" subtitle="Saved prompt instructions on this worker only. Changes apply to future prompts that load these blocks.">
	{#snippet children()}
		<RowGroup title="Shipped defaults">
			<SettingRow label="Built-in model guidance" hint="AI Shuffle ships model guidance in code. It stays in place when a saved block is edited or deleted." />
		</RowGroup>
		{#if loading}
			<p class="st-empty" role="status">Loading saved guidance…</p>
		{:else if error}
			<InlineBanner tone="warn">
				<span role="alert">{error}</span>
				{#snippet actions()}
					<button type="button" class="st-btn sm" onclick={load}>Retry</button>
				{/snippet}
			</InlineBanner>
		{:else if data && !data.blocks.length}
			<p class="st-empty">No saved guidance on this worker.</p>
		{:else if data}
			{#each data.blocks as block (block.id)}
				<GuidanceEditor
					initialBlock={block}
					maxChars={data.max_chars}
					{save}
					{reload}
					onDelete={async (current) => {
						await api.delete(`${path}/${current.id}?expected_revision=${current.revision}`);
						remove(current.id);
					}}
					onMissing={() => remove(block.id)}
				/>
			{/each}
		{/if}
	{/snippet}
</DetailPane>
