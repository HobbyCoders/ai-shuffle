<script lang="ts" module>
	export interface Block {
		id: string;
		stem: string;
		content: string;
		revision: string;
		scope: 'exact' | 'model_substring';
		model: string;
		provider: string | null;
		inactive_reason: string | null;
	}
</script>

<script lang="ts">
	import { getErrorMessage, isApiError } from '$lib/api/client';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';

	interface Props {
		initialBlock: Block;
		maxChars: number;
		save: (block: Block, content: string) => Promise<Block>;
		reload: (id: string) => Promise<Block | undefined>;
		onDelete: (block: Block) => Promise<void>;
		onMissing: () => void;
	}
	let { initialBlock, maxChars, save, reload, onDelete, onMissing }: Props = $props();
	// Keyed by block ID; retain the draft and its base revision until explicit reload.
	// svelte-ignore state_referenced_locally
	let block = $state(initialBlock);
	// svelte-ignore state_referenced_locally
	let draft = $state(initialBlock.content);
	let busy = $state(false);
	let conflict = $state(false);
	let error = $state('');
	const uid = $props.id();
	const length = $derived(Array.from(draft.trim()).length);
	const dirty = $derived(draft !== block.content);
	const title = $derived(block.scope === 'exact' ? block.model : block.stem);
	const invalid = $derived(!length || length > maxChars);

	async function change(action: () => Promise<void>, fallback: string) {
		if (busy) return;
		busy = true;
		error = '';
		try {
			await action();
		} catch (e) {
			conflict ||= isApiError(e) && e.status === 409;
			error = getErrorMessage(e, fallback);
		} finally {
			busy = false;
		}
	}

	function submit() {
		return change(async () => {
			block = await save(block, draft);
			draft = block.content;
			flashSaved();
		}, 'Could not save guidance.');
	}

	function refresh() {
		return change(async () => {
			const latest = await reload(block.id);
			if (!latest) return onMissing();
			block = latest;
			draft = latest.content;
			conflict = false;
		}, 'Could not reload guidance.');
	}
</script>

<RowGroup {title}>
	<SettingRow
		label="Applies to"
		hint={block.scope === 'exact' ? `${block.model} · ${block.provider} · learning account only` : `Model IDs containing “${block.model}” (case-insensitive)`}
	/>
	{#if block.inactive_reason}
		<InlineBanner tone="info">{block.inactive_reason}</InlineBanner>
	{/if}
	<SettingRow label="Saved prompt instructions">
		{#snippet expanded()}
			<textarea class="st-area guidance-text" aria-label={`Guidance for ${title}`} aria-describedby={uid} bind:value={draft} disabled={busy}></textarea>
			<div class="guidance-actions">
				<span id={uid}>{length} / {maxChars}{!length ? ' · Enter guidance' : length > maxChars ? ' · Too long' : ''}</span>
				<button type="button" class="st-btn primary" disabled={busy || conflict || invalid || !dirty} onclick={submit}>Save guidance</button>
			</div>
		{/snippet}
	</SettingRow>
	{#if error || conflict}
		<InlineBanner tone="warn">
			<span role="alert">{error}{conflict ? ' Your draft is still here. Loading the latest version replaces it.' : ''}</span>
			{#snippet actions()}
				{#if conflict}
					<button type="button" class="st-btn sm" disabled={busy} onclick={refresh}>Load latest</button>
				{/if}
			{/snippet}
		</InlineBanner>
	{/if}
	<SettingRow label="Delete this saved block">
		<InlineConfirm
			label="Delete guidance"
			confirmLabel="Confirm delete"
			question="Delete this block from this worker? Automatic learning won't restore it. Shipped defaults are not changed."
			onConfirm={() => change(() => onDelete(block), 'Could not delete guidance.')}
			disabled={busy || conflict}
		/>
	</SettingRow>
</RowGroup>

<style>
	.guidance-text {
		width: 100%;
		min-height: 106px;
		overflow-wrap: anywhere;
	}
	.guidance-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 8px;
		font-size: 12px;
		color: var(--st-mfg);
	}
</style>
