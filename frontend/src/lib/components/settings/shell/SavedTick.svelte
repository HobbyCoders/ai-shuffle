<script lang="ts">
	/** The "Saved" tick in a page or detail header, driven by `flashSaved()`. */
	import { Check } from 'lucide-svelte';
	import { saveStatus } from '$lib/settings/save-status.svelte';
</script>

<span class="st-saved" aria-live="polite">
	{#if saveStatus.visible}
		{#key saveStatus.count}
			<span class="in"><Check />Saved</span>
		{/key}
	{/if}
</span>

<style>
	.st-saved {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		min-width: 60px;
		justify-content: flex-end;
		font-size: 12.5px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
	.in {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		animation: st-saved-in 0.25s ease-out;
	}
	.in :global(svg.lucide) {
		color: var(--st-success);
		width: 14px;
		height: 14px;
	}
	@keyframes st-saved-in {
		from {
			opacity: 0;
		}
	}
</style>
