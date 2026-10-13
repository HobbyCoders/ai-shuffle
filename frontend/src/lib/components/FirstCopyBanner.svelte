<script lang="ts">
	/**
	 * A worker that joins a network starts empty and copies the network's data
	 * from another worker first. On a large network that takes many minutes,
	 * and until it finishes there are no sessions and no other workers to
	 * show. This banner says the copy is running and how far it got, and
	 * offers a reload once it is done. The first copy leaves transcripts out;
	 * while they download the banner counts sessions. Silent on a ready worker
	 * with nothing left to download; it then stops asking for status.
	 */
	import { onMount } from 'svelte';
	import { getNetworkStatus, type NetworkStatus } from '$lib/api/network';
	import { firstCopyNotice, transcriptNotice } from '$lib/api/network-status';
	import Banner from './Banner.svelte';
	import NotificationSlot from './NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	/** While the copy runs. */
	const COPY_POLL_MS = 3000;
	/** While transcripts download on a worker that is already usable. */
	const TRANSCRIPT_POLL_MS = 10_000;
	/** Signed in or invited, waiting for another worker to admit this one. */
	const JOIN_POLL_MS = 30_000;

	let status = $state<NetworkStatus | null>(null);
	let finished = $state(false);
	let dismissed = $state(false);
	const notice = $derived(firstCopyNotice(status) ?? transcriptNotice(status));

	let timer: ReturnType<typeof setTimeout> | undefined;
	let stopped = false;

	function nextDelay(current: NetworkStatus | null): number | null {
		if (!current) return null;
		if (firstCopyNotice(current)) return COPY_POLL_MS;
		if (transcriptNotice(current)) return TRANSCRIPT_POLL_MS;
		if (!current.enrolled && (current.hub?.linked || current.enrollment)) return JOIN_POLL_MS;
		return null;
	}

	function schedule() {
		const delay = nextDelay(status);
		if (stopped || delay === null) return;
		timer = setTimeout(() => {
			if (typeof document !== 'undefined' && document.visibilityState === 'hidden') schedule();
			else void load();
		}, delay);
	}

	async function load() {
		try {
			const next = await getNetworkStatus();
			if (stopped) return;
			const wasCopying = firstCopyNotice(status) !== null;
			status = next;
			if (wasCopying && next.network?.bootstrap_state === 'ready') finished = true;
		} catch {
			// Keep the last known status; without one there is nothing to show.
		}
		schedule();
	}

	onMount(() => {
		void load();
		return () => {
			stopped = true;
			clearTimeout(timer);
		};
	});
</script>

{#if notice}
	<NotificationSlot order={DOCK_ORDER.account}>
		<Banner
			variant="page"
			title={notice.title}
			severity={notice.severity}
			action={finished && !dismissed ? { label: 'Reload', onClick: () => window.location.reload() } : undefined}
		>
			<span class="copy-message">{notice.message}</span>
			{#if notice.fraction !== null}
				<progress class="copy-progress" max="1" value={notice.fraction} aria-label="Copy progress"></progress>
			{/if}
		</Banner>
	</NotificationSlot>
{:else if finished && !dismissed}
	<NotificationSlot order={DOCK_ORDER.account}>
		<Banner
			variant="page"
			severity="success"
			title="Your data is copied"
			message="Reload to see your sessions and workers."
			action={{ label: 'Reload', onClick: () => window.location.reload() }}
			dismissible
			onDismiss={() => (dismissed = true)}
		/>
	</NotificationSlot>
{/if}

<style>
	.copy-message {
		display: block;
	}

	.copy-progress {
		display: block;
		width: 100%;
		height: 4px;
		margin-top: 8px;
		appearance: none;
		border: 0;
		border-radius: 999px;
		overflow: hidden;
		background: var(--muted);
	}
	.copy-progress::-webkit-progress-bar {
		background: var(--muted);
	}
	.copy-progress::-webkit-progress-value {
		background: var(--primary);
	}
	.copy-progress::-moz-progress-bar {
		background: var(--primary);
	}
</style>
