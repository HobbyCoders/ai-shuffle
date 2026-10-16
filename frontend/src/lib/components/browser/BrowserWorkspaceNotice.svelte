<script lang="ts">
	/**
	 * A plain notice from the browser runtime, e.g. a page whose site withheld
	 * its captcha from this browser: the step has to be finished elsewhere.
	 */
	interface Props {
		notice?: string | null;
	}
	let { notice }: Props = $props();
</script>

{#if notice}
	<div class="workspace-notice" role="status">{notice}</div>
{/if}

<style>
	.workspace-notice {
		position: absolute;
		left: 8px;
		right: 8px;
		bottom: 8px;
		z-index: 3;
		padding: 8px 10px;
		font-size: 12px;
		line-height: 1.5;
		background: var(--background);
		color: var(--foreground);
		border: 1px solid var(--border);
		border-left: 3px solid var(--primary);
		border-radius: 6px;
		box-shadow: 0 4px 12px #0002;
	}
</style>
