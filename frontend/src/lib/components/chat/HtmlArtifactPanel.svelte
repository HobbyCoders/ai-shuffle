<script lang="ts">
	/** Side-panel view: the artifact beside the conversation, at full panel height. */
	import type { Snippet } from 'svelte';
	import HtmlArtifactViewer from './HtmlArtifactViewer.svelte';
	import { HtmlArtifactSource } from './html-artifact-source.svelte';

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
		onReload={file.reload} actions={panelActions} />
</section>

<style>
	.artifact-panel { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--background); }
	.artifact-panel.overlay { padding-top: env(safe-area-inset-top, 0px); }
</style>
