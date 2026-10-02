<script lang="ts">
	/**
	 * DeviceCodeSetup: the ChatGPT / SuperGrok device-code sign-in, inline in a
	 * credential's detail. Starts on mount (arriving here is the user's start).
	 * Draft mode creates the row and deletes it on Cancel; reconnect mode signs
	 * an existing row in again and never deletes it. See device-enrollment.
	 */
	import { onDestroy, onMount, untrack } from 'svelte';
	import { Check, Copy, ExternalLink, TriangleAlert } from 'lucide-svelte';
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	import { DeviceEnrollment, type DeviceKind } from '../device-enrollment.svelte';

	interface Props {
		kind: DeviceKind;
		existingProviderId?: string | null;
		/** Resume a draft's placeholder row (still owned: Cancel deletes it). */
		draftRowId?: string | null;
		onConnected?: (providerId: string) => void;
		onDone?: () => void;
		onCancel?: () => void;
		/** Reports the row id as soon as a draft is created. */
		onRow?: (providerId: string | null) => void;
	}

	let { kind, existingProviderId, draftRowId, onConnected, onDone, onCancel, onRow }: Props = $props();

	// Ownership belongs to this enrollment, not to later prop updates.
	const enrollment = untrack(
		() =>
			new DeviceEnrollment({
				kind,
				existingProviderId,
				draftRowId,
				onConnected: onConnected ? (id) => onConnected?.(id) : undefined,
				onDone: () => onDone?.(),
				onCancel: () => onCancel?.()
			})
	);

	let copied = $state(false);
	let copyError = $state('');
	let copyTimer: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		onRow?.(enrollment.providerId);
	});

	const service = $derived(kind === 'codex' ? 'ChatGPT' : 'SuperGrok');

	async function copyCode() {
		if (!enrollment.userCode) return;
		copied = false;
		copyError = '';
		try {
			await copyText(enrollment.userCode);
			copied = true;
			if (copyTimer !== null) clearTimeout(copyTimer);
			copyTimer = setTimeout(() => (copied = false), 2000);
		} catch {
			copyError = COPY_ERROR_MESSAGE;
		}
	}

	onMount(() => enrollment.start());
	onDestroy(() => {
		enrollment.dispose();
		if (copyTimer !== null) clearTimeout(copyTimer);
	});
</script>

<div class="pv-card">
	<div class="pv-flow" data-testid="device-code-setup">
		{#if enrollment.phase === 'awaiting'}
			<div class="pv-flow-head">Enter this code at {service}</div>
			<div class="pv-row-inline">
				<span class="pv-bigcode">{enrollment.userCode}</span>
				<button type="button" class="st-btn sm" onclick={copyCode}>
					{#if copied}<Check />Copied{:else}<Copy />Copy{/if}
				</button>
				<a class="st-btn sm" href={enrollment.verificationUri} target="_blank" rel="noopener">
					<ExternalLink />Open sign-in page
				</a>
			</div>
			{#if copyError}<p class="pv-msg err" role="alert">{copyError}</p>{/if}
			<div class="pv-wait"><span class="pv-spin" aria-hidden="true"></span>Waiting for authorization</div>
			<div class="pv-row-inline">
				<button type="button" class="st-btn ghost" onclick={() => enrollment.cancel()}>Cancel</button>
			</div>
		{:else if enrollment.phase === 'error'}
			<p class="err" role="alert"><TriangleAlert />{enrollment.errorMessage}</p>
			<div class="pv-row-inline">
				<button type="button" class="st-btn" onclick={() => enrollment.cancel()}>
					{enrollment.ownsRow ? 'Remove draft' : 'Close'}
				</button>
				<button type="button" class="st-btn primary" onclick={() => enrollment.tryAgain()}>Try again</button>
			</div>
		{:else}
			<div class="pv-wait" role="status">
				<span class="pv-spin" aria-hidden="true"></span>{enrollment.phase === 'finishing'
					? 'Finishing sign-in'
					: 'Preparing sign-in'}
			</div>
		{/if}
	</div>
</div>

<style>
	.err {
		display: flex;
		gap: 8px;
		align-items: flex-start;
		margin: 0;
		font-size: 13px;
		color: var(--st-destructive);
	}
	.err :global(svg.lucide) {
		width: 15px;
		height: 15px;
		margin-top: 1px;
	}
	a.st-btn {
		text-decoration: none;
		color: inherit;
	}
</style>
