<script lang="ts">
	/**
	 * Side-panel view: the artifact beside the conversation, at full panel
	 * height. Esc pressed inside the page closes the panel, as it does
	 * everywhere else in it (the page holds focus, so the panel's own key
	 * handler never sees it).
	 */
	import type { Snippet } from 'svelte';
	import HtmlArtifactViewer from './HtmlArtifactViewer.svelte';
	import { HtmlArtifactSource } from './html-artifact-source.svelte';
	import { htmlArtifactPanel } from '$lib/stores/html-artifact-panel.svelte';

	let { href, filename, sessionId, panelActions, mobile: overlay = false }: {
		href: string;
		filename: string;
		sessionId: string;
		panelActions?: Snippet;
		/** Full-screen overlay on phones: clear the status bar. */
		mobile?: boolean;
	} = $props();

	const file = new HtmlArtifactSource(() => ({ href, sessionId }));
	let view = $state<'preview' | 'source'>('preview');
	let mobile = $state(false);
</script>

<section class="artifact-panel" class:overlay aria-label={`HTML artifact: ${filename}`}>
	<HtmlArtifactViewer {filename} source={file.source} loading={file.loading} error={file.error}
		downloadUrl={file.downloadUrl} revision={file.revision} bind:view bind:mobile variant="panel"
		onReload={file.reload} onEscape={() => htmlArtifactPanel.close(sessionId)} actions={panelActions} />
</section>

<style>
	.artifact-panel { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--background); }
	.artifact-panel.overlay { padding-top: env(safe-area-inset-top, 0px); }
</style>
