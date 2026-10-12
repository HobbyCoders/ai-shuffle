<script lang="ts">
	/**
	 * MessageArea's session-switch save path, reduced to what decides the saved
	 * reading position: a scroll frame records the position, and a `$effect`
	 * (which runs AFTER the DOM shows the entered session) files the leaving
	 * session's position. The lines inside the effect mirror MessageArea; the
	 * reading-position source test pins that MessageArea still reads this way.
	 */
	import { untrack } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { createScrollTopPersist } from '$lib/components/chat/scroll-top-persist';
	import { sessionEphemeralCache } from '$lib/stores/session-cache';

	interface Props {
		session: Writable<{ sessionId: string; rows: string[]; scrollTop: number }>;
		scrollPositionCache: Map<string, number>;
		onContainer?: (el: HTMLDivElement) => void;
	}
	let { session, scrollPositionCache, onContainer }: Props = $props();

	let containerRef = $state<HTMLDivElement | null>(null);
	let prevTrackedSessionId = $state<string | undefined>(undefined);

	const scrollTopPersist = createScrollTopPersist({
		delayMs: 250,
		currentKey: () => $session.sessionId,
		writeCurrent: (position) => session.update((s) => ({ ...s, scrollTop: position })),
		writeLeft: (key, position) => sessionEphemeralCache.patchScrollTop(key, position)
	});

	export function scrollFrame() {
		if (!containerRef) return;
		const cacheKey = $session.sessionId;
		scrollPositionCache.set(cacheKey, containerRef.scrollTop);
		scrollTopPersist.schedule(cacheKey, containerRef.scrollTop);
	}

	$effect(() => {
		if (containerRef) onContainer?.(containerRef);
	});

	$effect(() => {
		const currentId = $session.sessionId;
		const prevId = untrack(() => prevTrackedSessionId);
		if (currentId === prevId) return;
		if (prevId) {
			const leftAt = scrollTopPersist.leave(prevId, scrollPositionCache.get(prevId));
			if (leftAt !== undefined) scrollPositionCache.set(prevId, leftAt);
		} else {
			scrollTopPersist.cancel();
		}
		prevTrackedSessionId = currentId;
	});
</script>

<div bind:this={containerRef} data-testid="scroller">
	{#each $session.rows as row (row)}
		<div class="row">{row}</div>
	{/each}
</div>
