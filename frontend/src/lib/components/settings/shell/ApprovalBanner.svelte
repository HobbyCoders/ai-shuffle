<script lang="ts">
	/**
	 * The one read-only notice: this paired device is not approved yet, so
	 * every setting shows but nothing can change. Sits above the content.
	 */
	import { ShieldAlert } from 'lucide-svelte';
	import LinkValue from '../rows/LinkValue.svelte';
</script>

<div class="st-appr" role="status">
	<ShieldAlert />
	<b>This device needs approval</b>
	<span class="why">Approve from an approved device</span>
	<span class="go"><LinkValue label="Devices" ariaLabel="Open Devices" target={{ category: 'devices' }} /></span>
</div>

<style>
	.st-appr {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 9px 20px;
		background: var(--st-warning-soft);
		border-bottom: 1px solid var(--st-border);
		font-size: 13px;
		flex: none;
	}
	.st-appr > :global(svg.lucide) {
		color: var(--st-warning);
	}
	b {
		font-weight: 600;
	}
	.why {
		color: var(--st-mfg);
	}
	.go {
		margin-left: auto;
		display: inline-flex;
	}
	.go :global(.st-vlink) {
		color: var(--st-fg);
	}
</style>
