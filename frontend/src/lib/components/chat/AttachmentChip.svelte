<script lang="ts">
	/** An upload a sent or queued user message carries, drawn in place of its @path. */
	import { uploadFileName } from '$lib/utils/message-presentation';

	let { path }: { path: string } = $props();
	const name = $derived(uploadFileName(path));
</script>

<span class="attachment-chip" title={name}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><polyline points="13,2 13,9 20,9" /></svg><span class="attachment-chip-name">{name}</span></span>

<style>
	/* Matches the composer's file chip (TiptapEditor's .file-chip-node). */
	.attachment-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		max-width: 100%;
		padding: 0.125rem 0.5rem;
		margin: 0 0.125rem;
		background: var(--chip-primary-bg);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		font-size: 0.75rem;
		line-height: 1.4;
		vertical-align: middle;
	}

	.attachment-chip svg {
		flex-shrink: 0;
		width: 12px;
		height: 12px;
		opacity: 0.7;
	}

	.attachment-chip-name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
