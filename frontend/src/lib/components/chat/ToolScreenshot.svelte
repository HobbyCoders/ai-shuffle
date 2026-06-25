<script lang="ts">
	/**
	 * ToolScreenshot — renders a tool's screenshot (e.g. a Claude-in-Chrome MCP
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
</script>

<div class="tool-screenshot">
	{@html sanitizeHtml(mediaHtml('image', screenshot, `Screenshot from ${name}`))}
</div>

<style>
	/* The inner .generated-media-container supplies its own spacing/border/shadow. */
	.tool-screenshot {
		display: block;
	}
</style>
