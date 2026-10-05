<script lang="ts">
	/**
	 * ToolScreenshot — renders a tool's screenshot (e.g. a browser-automation MCP
	 * image result) using the shared media UI, so it gets the same expand button,
	 * download button, and click-to-open lightbox as generated images.
	 *
	 * Rendered directly below the tool call (ToolUseMessage) when a tool stands
	 * alone, OR below the whole ToolUseGroup when tools are grouped/collapsed —
	 * either way it sits OUTSIDE the collapsible <details> so it's visible
	 * regardless of expand state (a screenshot the user can't see is useless).
	 */
	import { mediaHtml, resolveMediaUrl, retryImageLoad } from '$lib/utils/media';
	import { sanitizeHtml } from '$lib/utils/sanitize';

	interface Props {
		/** Data URI / URL of the screenshot image. */
		screenshot: string;
		/** Tool name, used for alt/aria text ("Screenshot from <name>"). */
		name: string;
		sessionId?: string | null;
	}

	let { screenshot, name, sessionId }: Props = $props();
	const source = $derived(resolveMediaUrl(screenshot, sessionId));

	let container: HTMLElement | undefined = $state();

	// Artifact-store screenshots (`/api/v1/artifacts/{hash}`) can 404 briefly
	// (fire-and-forget store; a viewer on another worker syncs the row a pass
	// after the event). Retry a served (non-data:) URL with backoff.
	$effect(() => {
		const img = container?.querySelector('img');
		if (!img) return;
		return retryImageLoad(img, source);
	});
</script>

<div class="tool-screenshot" bind:this={container}>
	{@html sanitizeHtml(mediaHtml('image', source, `Screenshot from ${name}`))}
</div>

<style>
	/* The inner .generated-media-container supplies its own spacing/border/shadow. */
	.tool-screenshot {
		display: block;
	}
</style>
