<script lang="ts">
	import { activeSession } from '$lib/stores/active-session';
	import type { ChatSurface } from '$lib/components/layout/chat-empty-state';
	import TranscriptHydrationBoundary from '../TranscriptHydrationBoundary.svelte';
	import TranscriptPresentationProbe from './TranscriptPresentationProbe.svelte';
	let { graceMs, mirrorPending = false, navigationOverlayOpen = false, onSurface }: {
		graceMs?: number; mirrorPending?: boolean; navigationOverlayOpen?: boolean; onSurface?: (surface: ChatSurface, messageCount: number, sessionId: string | null | undefined) => void;
	} = $props();
</script>
<div data-testid="mirror-owner" inert={mirrorPending}>
	<TranscriptHydrationBoundary {graceMs} {mirrorPending} {navigationOverlayOpen} {onSurface}>
		<TranscriptPresentationProbe />
	</TranscriptHydrationBoundary>
</div>
<input aria-label="Bound composer" data-session={$activeSession.sessionId} value="draft" />
<div data-testid="permissions">{$activeSession.pendingPermissions[0]?.session_id ?? 'none'}</div>
