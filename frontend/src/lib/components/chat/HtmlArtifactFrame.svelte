<script lang="ts">
	/**
	 * The sandboxed frame every HTML artifact view uses: inline in the reply,
	 * the expanded dialog and the side panel. It owns the srcdoc, the live
	 * theme and the message bridge; callers own size and chrome.
	 *
	 * Security: srcdoc only, sandbox="allow-scripts" (opaque origin, never
	 * allow-same-origin), CSP first. Messages are accepted only from this
	 * frame's own window with the opaque origin "null"; links and images also
	 * need the reader's focus and a fresh user activation.
	 */
	import { untrack } from 'svelte';
	import {
		acceptHtmlPreviewMessage,
		buildHtmlPreviewDocument,
		hostContextMessage,
		previewHasUserIntent,
		HTML_PREVIEW_SANDBOX
	} from '$lib/utils/htmlPreview';
	import { currentHostTheme, watchHostTheme } from '$lib/utils/htmlPreviewTheme';
	import { openLightbox } from '$lib/stores/lightbox';

	let {
		source,
		title,
		display = 'inline',
		scrollable = false,
		allowImages = true,
		class: className = '',
		onSize,
		onLoad,
		onEscape,
		onHover,
		onScrollTo
	}: {
		source: string;
		title: string;
		display?: 'inline' | 'fullscreen';
		/** Inline frames only: let the page scroll (a fixed-height fallback). */
		scrollable?: boolean;
		/** The lightbox sits under a modal dialog's top layer, so dialogs turn this off. */
		allowImages?: boolean;
		class?: string;
		onSize?: (height: number, viewport?: number) => void;
		onLoad?: () => void;
		onEscape?: () => void;
		onHover?: (inside: boolean) => void;
		onScrollTo?: (top: number) => void;
	} = $props();

	let frame = $state<HTMLIFrameElement | null>(null);
	// Raw, not a deep proxy: the theme object is posted to the frame, and
	// structured cloning cannot copy a reactive proxy.
	let theme = $state.raw(currentHostTheme());
	$effect(() => watchHostTheme((next) => { theme = next; }));

	// The first paint uses the theme of the moment; later changes travel by
	// message, so a theme switch never reloads the page or loses its state.
	const srcdoc = $derived(buildHtmlPreviewDocument(source, { theme: untrack(() => theme), display }));

	function pushContext() {
		frame?.contentWindow?.postMessage(hostContextMessage(theme, display === 'inline' ? scrollable : undefined), '*');
	}
	$effect(() => {
		void theme; void scrollable;
		untrack(pushContext);
	});

	$effect(() => {
		function onMessage(event: MessageEvent) {
			const message = acceptHtmlPreviewMessage(event, frame);
			if (!message) return;
			if (message.kind === 'size') onSize?.(message.height, message.viewport);
			else if (message.kind === 'link') {
				if (previewHasUserIntent(frame)) window.open(message.url, '_blank', 'noopener,noreferrer');
			} else if (message.kind === 'image') {
				if (allowImages && previewHasUserIntent(frame)) openLightbox('image', message.src);
			} else if (message.kind === 'scroll') {
				if (previewHasUserIntent(frame)) onScrollTo?.(message.top);
			} else if (message.kind === 'hover') {
				onHover?.(message.inside);
			} else if (message.kind === 'escape') {
				// Escape is not a user activation, so focus is the whole gate.
				if (document.activeElement === frame) onEscape?.();
			}
		}
		window.addEventListener('message', onMessage);
		return () => window.removeEventListener('message', onMessage);
	});
</script>

<!-- color-scheme matches the page's injected scheme, so the browser never paints
     an opaque white backdrop behind a transparent page in dark mode. -->
<iframe
	bind:this={frame}
	{title}
	{srcdoc}
	sandbox={HTML_PREVIEW_SANDBOX}
	referrerpolicy="no-referrer"
	allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
	class={className}
	style:color-scheme={theme.appearance}
	onload={() => { pushContext(); onLoad?.(); }}
></iframe>

<style>
	iframe { display: block; width: 100%; height: 100%; border: 0; background: transparent; }
</style>
