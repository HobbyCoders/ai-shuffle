<script lang="ts">
	/**
	 * PermissionBanner - Permission request UI with allow/deny buttons
	 *
	 * Wraps PermissionQueue in a styled banner container that appears
	 * in the chat area when permission requests are pending.
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
	<div class="permission-banner">
		<div class="max-w-5xl mx-auto px-4 sm:px-8 py-3">
			<div class="permission-panel">
				<PermissionQueue {requests} on:respond={onRespond} />
			</div>
		</div>
	</div>
{/if}

<style>
	.permission-banner {
		animation: banner-enter 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	.permission-panel {
		border-radius: 0.75rem;
		padding: 0;
	}

	@keyframes banner-enter {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* Mobile: Bottom sheet style */
	@media (max-width: 639px) {
		.permission-banner {
			position: fixed;
			bottom: 0;
			left: 0;
			right: 0;
			z-index: 50;
			padding-bottom: env(safe-area-inset-bottom, 0);
			background: var(--background);
			border-radius: 1.25rem 1.25rem 0 0;
			box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.15);
			animation: sheet-enter 0.3s cubic-bezier(0.16, 1, 0.3, 1);
		}

		@keyframes sheet-enter {
			from {
				opacity: 0;
				transform: translateY(100%);
			}
			to {
				opacity: 1;
				transform: translateY(0);
			}
		}
	}
</style>
