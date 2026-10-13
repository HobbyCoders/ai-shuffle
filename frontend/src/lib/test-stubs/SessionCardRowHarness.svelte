<script lang="ts">
	// Mounts a SessionCard the way Sidebar.svelte does: only `session` follows the
	// session list, every other prop is fixed. Replacing `session` here is what a
	// list reload does to each keyed row.
	import type { Session } from '$lib/api/client';
	import SessionCard from '$lib/components/layout/SessionCard.svelte';

	let { session }: { session: Session } = $props();
	const now = Date.parse('2026-08-20T12:06:00Z');
	const onOpen = () => {};
</script>

<SessionCard {session} status="done" projectLabel="Project A" active={false} {now} {onOpen} />
