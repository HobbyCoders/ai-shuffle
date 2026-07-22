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
	import { mediaHtml } from '$lib/utils/media';
	import { sanitizeHtml } from '$lib/utils/sanitize';

	interface Props {
		/** Data URI / URL of the screenshot image. */
		screenshot: string;
		/** Tool name, used for alt/aria text ("Screenshot from <name>"). */
		name: string;
	}

	let { screenshot, name }: Props = $props();

	let container: HTMLElement | undefined = $state();

	// Artifact-store screenshots (`/api/v1/artifacts/{hash}`) are written
	// fire-and-forget by the recorder, so the first fetch can race the disk/DB
	// store and 404. Retry a served (non-data:) URL a few times with backoff
	// before giving up.
	const MAX_RETRIES = 3;
	$effect(() => {
		if (!container || screenshot.startsWith('data:')) return;
		const img = container.querySelector('img');
		if (!img) return;
		let attempts = 0;
		let timer: ReturnType<typeof setTimeout> | undefined;
		const onError = () => {
			if (attempts >= MAX_RETRIES) return;
			attempts += 1;
			timer = setTimeout(() => {
				img.src = `${screenshot}${screenshot.includes('?') ? '&' : '?'}retry=${attempts}`;
			}, 500 * 2 ** (attempts - 1));
		};
		img.addEventListener('error', onError);
		return () => {
			img.removeEventListener('error', onError);
			if (timer !== undefined) clearTimeout(timer);
		};
	});
</script>

<div class="tool-screenshot" bind:this={container}>
	{@html sanitizeHtml(mediaHtml('image', screenshot, `Screenshot from ${name}`))}
</div>

<style>
	/* The inner .generated-media-container supplies its own spacing/border/shadow. */
	.tool-screenshot {
		display: block;
	}
</style>
