<script lang="ts">
	/**
	 * PermissionBanner - Permission request UI with allow/deny buttons
	 */
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import { type PermissionRequestData } from '$lib/components/PermissionRequest.svelte';

	interface Props {
		requests: PermissionRequestData[];
		onRespond: (event: CustomEvent<{
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		}>) => void;
	}

	let { requests, onRespond }: Props = $props();
</script>

{#if requests && requests.length > 0}
	<div class="border-t border-warning/30 bg-warning/5 p-3 sm:p-4">
		<div class="max-w-5xl mx-auto">
			<PermissionQueue {requests} on:respond={onRespond} />
		</div>
	</div>
{/if}
