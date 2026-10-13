<script lang="ts">
	/** Test double for $lib MainPanel: renders the shell's header snippet with a fixed context. */
	import type { Snippet } from 'svelte';
	import type { MainPanelHeaderContext } from '$lib/components/layout/main-panel-header';
	import { mainPanelView } from './main-panel-view';

	let { onOpenSession, header }: {
		onOpenSession: (sessionId: string) => void;
		header?: Snippet<[MainPanelHeaderContext]>;
	} = $props();
</script>

{#snippet downloadAction()}<button type="button">Download chat</button>{/snippet}
{#snippet headerActions()}<button type="button">Commit</button>{/snippet}

<div data-testid="main-panel">
	{@render header?.({ title: 'T', ...mainPanelView, downloadAction, headerActions })}
	<button type="button" onclick={() => onOpenSession('s-9')}>Panel open session</button>
</div>
