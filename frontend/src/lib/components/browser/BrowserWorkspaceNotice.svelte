<script lang="ts">
	import { InlineBanner } from '$lib/components/layout/view';
	import '../ui/surfaces.css';
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
	<div class="workspace-notice ui-surface" role="status"><InlineBanner tone="info">{notice}</InlineBanner></div>
{/if}

<style>
	.workspace-notice {
		position: absolute;
		left: 8px;
		right: 8px;
		bottom: 8px;
		z-index: 3;

	}
</style>
