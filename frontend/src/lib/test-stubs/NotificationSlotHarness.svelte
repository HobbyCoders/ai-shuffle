<script lang="ts">
	/** Test double: one NotificationSlot holding a plain card with `label`. */
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	let { label, order = DOCK_ORDER.banners }: { label: string; order?: number } = $props();
</script>

<NotificationSlot {order}><div class="card">{label}</div></NotificationSlot>
