<script lang="ts">
	/**
	 * Draws its content in the shared NotificationDock instead of where it is
	 * written, so every notice shows in the same place (#1823). Wrap exactly
	 * one card element: the dock makes that element, and nothing around it,
	 * the click target, so the clickable box is the drawn box.
	 *
	 *   {#if visible}
	 *     <NotificationSlot order={DOCK_ORDER.voiceCall}><aside class="card">…</aside></NotificationSlot>
	 *   {/if}
	 */
	import type { Snippet } from 'svelte';
	import { notificationDock, DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	let { order = DOCK_ORDER.banners, children }: { order?: number; children: Snippet } = $props();

	$effect(() => {
		const id = notificationDock.add(order, children);
		return () => notificationDock.remove(id);
	});
</script>

{#if !notificationDock.hasHost}
	{@render children()}
{/if}
