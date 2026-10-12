<script lang="ts">
	/**
	 * WorkLogGroup whose props a test can replace WITHOUT flushing, so an event
	 * can reach the group while an update is queued: the DOM, and the run the
	 * reader clicked, are still the previous render's.
	 */
	import type { ComponentProps } from 'svelte';
	import WorkLogGroup from '../WorkLogGroup.svelte';

	type Props = ComponentProps<typeof WorkLogGroup>;
	let { initial }: { initial: Props } = $props();
	// svelte-ignore state_referenced_locally
	let current = $state(initial);
	export function queue(next: Props) {
		current = next;
	}
</script>

<WorkLogGroup {...current} />
