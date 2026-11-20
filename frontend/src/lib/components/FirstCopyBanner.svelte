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
	import { onMount, tick } from 'svelte';
	import { ChevronUp, Download } from 'lucide-svelte';
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

	let { completionFocusTarget }: { completionFocusTarget?: HTMLElement } = $props();

	let status = $state<NetworkStatus | null>(null);
	let finished = $state(false);
	let dismissed = $state(false);
	let minimized = $state(false);
	let chip = $state<HTMLButtonElement>();
	let details = $state<HTMLDivElement>();
	let copyMessage = $state<HTMLSpanElement>();
	const firstCopy = $derived(firstCopyNotice(status));
	const transcripts = $derived(transcriptNotice(status));
	const notice = $derived(firstCopy ?? transcripts);
	const canMinimize = $derived(!firstCopy && transcripts !== null);
	const showChip = $derived(canMinimize && minimized);

	async function minimize() {
		minimized = true;
		await tick();
		chip?.focus();
	}

	async function restore() {
		minimized = false;
		await tick();
		copyMessage?.focus();
	}

	function focusCompletionTarget() {
		const target = completionFocusTarget;
		if (!target) return;
		const temporary = !target.hasAttribute('tabindex');
		if (temporary) target.tabIndex = -1;
		target.focus({ preventScroll: true });
		if (!temporary) return;

		// Removing tabindex immediately drops focus. Clear it when focus leaves
		// so clicking empty Workspace doesn't take focus away from other controls.
		const reset = () => target.removeAttribute('tabindex');
		if (document.activeElement === target) target.addEventListener('blur', reset, { once: true });
		else reset();
	}

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
			const wasCopying = firstCopy !== null;
			const nextTranscripts = transcriptNotice(next);
			const completing = canMinimize && !nextTranscripts && !firstCopyNotice(next);
			const focused = completing ? document.activeElement : null;
			const hadFocus = focused && (focused === chip || details?.contains(focused));
			status = next;
			if (!nextTranscripts) minimized = false;
			if (wasCopying && next.network?.bootstrap_state === 'ready') finished = true;
			if (hadFocus) {
				await tick();
				// Move focus only if the notice was removed and nothing else took focus.
				if (!stopped && !focused.isConnected && document.activeElement === document.body) {
					focusCompletionTarget();
				}
			}
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

{#if notice && !showChip}
	<NotificationSlot order={DOCK_ORDER.account}>
		<!-- The dock can still read the snippet's props just before it removes the card. -->
		<Banner
			bind:element={details}
			variant="page"
			title={notice?.title}
			severity={notice?.severity}
			action={finished && !dismissed ? { label: 'Reload', onClick: () => window.location.reload() } : undefined}
			secondaryAction={canMinimize ? { label: 'Minimize', onClick: minimize } : undefined}
		>
			<span class="copy-message" tabindex="-1" bind:this={copyMessage}>{notice?.message}</span>
			{#if notice?.fraction != null}
				<progress class="copy-progress" max="1" value={notice?.fraction ?? 0} aria-label="Copy progress"></progress>
			{/if}
		</Banner>
	</NotificationSlot>
{:else if !notice && finished && !dismissed}
	<NotificationSlot order={DOCK_ORDER.account}>
		<Banner
			variant="page"
			severity="success"
			title="Your data is copied"
			message="Reload to see your chats and workers."
			action={{ label: 'Reload', onClick: () => window.location.reload() }}
			dismissible
			onDismiss={() => (dismissed = true)}
		/>
	</NotificationSlot>
{/if}

{#if showChip && transcripts}
	{@const progress = `${transcripts.severity === 'warning' ? 'Paused · ' : ''}${Math.floor((transcripts.fraction ?? 0) * 100)}%`}
	<button
		class="transcript-chip"
		type="button"
		aria-label={`${transcripts.title} · ${progress}. Restore details`}
		bind:this={chip}
		onclick={restore}
	>
		<Download size={14} aria-hidden="true" />
		<span>{transcripts.title} · {progress}</span>
		<ChevronUp size={14} aria-hidden="true" />
	</button>
{/if}

<style>
	.transcript-chip {
		display: inline-flex;
		align-items: center;
		align-self: flex-end;
		flex: none;
		gap: 6px;
		min-height: 36px;
		max-width: calc(100% - 24px);
		margin: 4px max(12px, env(safe-area-inset-right)) max(6px, env(safe-area-inset-bottom)) 12px;
		padding: 4px 10px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--card);
		color: var(--foreground);
		font-family: inherit;
		font-size: 0.75rem;
		font-weight: 600;
		cursor: pointer;
	}
	.transcript-chip:hover {
		background: var(--accent);
	}
	.transcript-chip:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.transcript-chip :global(svg) {
		flex: none;
	}

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
