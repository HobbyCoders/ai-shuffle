<script lang="ts">
	import { onMount } from 'svelte';
	import { RefreshCw, RotateCcw, X } from 'lucide-svelte';
	import {
		consumeCodexRateLimitResetCredit,
		listCodexRateLimitResetCredits,
		type CodexRateLimitResetCredit,
		type ConsumeCodexRateLimitResetCreditResponse,
		type ModelProvider
	} from '$lib/api/model-providers';
	import { getErrorMessage, isApiError } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';

	interface Props {
		provider: ModelProvider;
		onClose: () => void;
	}

	let { provider, onClose }: Props = $props();

	let dialogEl = $state<HTMLDivElement | null>(null);
	let cardEl = $state<HTMLDivElement | null>(null);
	let viewportWidth = $state(typeof window !== 'undefined' ? window.innerWidth : 1280);
	let fullscreen = $derived(isPanelFullscreenWidth(viewportWidth));

	let credits = $state<CodexRateLimitResetCredit[]>([]);
	let availableCount = $state(0);
	let hasLoaded = $state(false);
	let loading = $state(true);
	let refreshing = $state(false);
	let inventoryError = $state<string | null>(null);
	let consumeError = $state<string | null>(null);
	let armedCreditId = $state<string | null>(null);
	let consumingCreditId = $state<string | null>(null);
	let pendingAttempt = $state<{ creditId: string; idempotencyKey: string } | null>(null);
	let outcomeMessage = $state<string | null>(null);
	let liveMessage = $state('Loading banked resets…');

	let consumeActive = $derived(consumingCreditId !== null);
	let detailedAvailableCount = $derived(credits.filter((credit) => credit.status === 'available').length);

	const dateFormatter = new Intl.DateTimeFormat(undefined, {
		dateStyle: 'medium',
		timeStyle: 'short'
	});

	onMount(() => {
		const onResize = () => (viewportWidth = window.innerWidth);
		window.addEventListener('resize', onResize);
		queueMicrotask(() => dialogEl?.focus());
		void refreshInventory();
		return () => window.removeEventListener('resize', onResize);
	});

	function formatUnixSeconds(value: number | null): string | null {
		if (value === null || !Number.isFinite(value)) return null;
		const date = new Date(value * 1000);
		if (Number.isNaN(date.getTime())) return null;
		return dateFormatter.format(date);
	}

	function creditTitle(credit: CodexRateLimitResetCredit): string {
		return credit.title?.trim() || 'Codex rate-limit reset';
	}

	function statusLabel(credit: CodexRateLimitResetCredit): string {
		if (credit.status === 'available') return 'Available';
		if (credit.status === 'redeeming') return 'Being used';
		if (credit.status === 'redeemed') return 'Used';
		return 'Status unavailable';
	}

	function canUse(credit: CodexRateLimitResetCredit): boolean {
		return credit.reset_type === 'codex_rate_limits' && credit.status === 'available';
	}

	async function refreshInventory(): Promise<void> {
		if (consumeActive) return;
		if (hasLoaded) refreshing = true;
		else loading = true;
		inventoryError = null;
		liveMessage = hasLoaded ? 'Refreshing banked resets…' : 'Loading banked resets…';

		try {
			const result = await listCodexRateLimitResetCredits(provider.id);
			credits = result.credits;
			availableCount = result.available_count;
			hasLoaded = true;
			liveMessage = `${result.available_count} banked ${result.available_count === 1 ? 'reset' : 'resets'} available.`;
		} catch (error) {
			inventoryError = getErrorMessage(error);
			liveMessage = `Banked resets could not be loaded. ${inventoryError}`;
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	function armCredit(creditId: string): void {
		if (consumeActive) return;
		if (pendingAttempt?.creditId !== creditId) pendingAttempt = null;
		armedCreditId = creditId;
		consumeError = null;
	}

	function cancelCredit(creditId: string): void {
		if (consumeActive) return;
		if (armedCreditId === creditId) armedCreditId = null;
		if (pendingAttempt?.creditId === creditId) pendingAttempt = null;
		consumeError = null;
	}

	function messageForOutcome(result: ConsumeCodexRateLimitResetCreditResponse): string {
		if (result.outcome === 'reset') {
			return `${result.windows_reset} eligible Codex ${result.windows_reset === 1 ? 'window was' : 'windows were'} reset.`;
		}
		if (result.outcome === 'nothing_to_reset') {
			return 'No eligible Codex windows needed a reset.';
		}
		if (result.outcome === 'no_credit') {
			return 'This banked reset is no longer available.';
		}
		return 'This banked reset was already used.';
	}

	async function consumeCredit(creditId: string): Promise<void> {
		if (consumeActive) return;

		const attempt =
			pendingAttempt?.creditId === creditId
				? pendingAttempt
				: { creditId, idempotencyKey: crypto.randomUUID() };
		pendingAttempt = attempt;
		consumingCreditId = creditId;
		consumeError = null;
		outcomeMessage = null;
		liveMessage = 'Using banked reset…';

		try {
			const result = await consumeCodexRateLimitResetCredit(provider.id, {
				credit_id: creditId,
				idempotency_key: attempt.idempotencyKey
			});
			outcomeMessage = messageForOutcome(result);
			liveMessage = outcomeMessage;
			armedCreditId = null;
			pendingAttempt = null;
			consumingCreditId = null;
			await refreshInventory();
		} catch (error) {
			consumeError = getErrorMessage(error);
			liveMessage = `The banked reset could not be used. ${consumeError}`;
			if (isApiError(error) && error.status === 409) {
				try {
					await providers.refreshProviders();
				} catch {
					// Keep the consume error as the actionable message if provider refresh
					// also fails; the same idempotency key remains ready for retry.
				}
			}
		} finally {
			consumingCreditId = null;
		}
	}

	function requestClose(): void {
		if (!consumeActive) onClose();
	}

	function focusableElements(): HTMLElement[] {
		if (!cardEl) return [];
		return Array.from(
			cardEl.querySelectorAll<HTMLElement>(
				'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
			)
		).filter((element) => !element.hasAttribute('hidden'));
	}

	function handleDialogKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			requestClose();
			return;
		}
		if (event.key !== 'Tab') return;

		const focusable = focusableElements();
		if (focusable.length === 0) {
			event.preventDefault();
			dialogEl?.focus();
			return;
		}

		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		const active = document.activeElement;
		if (event.shiftKey && (active === first || active === dialogEl)) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && (active === last || active === dialogEl)) {
			event.preventDefault();
			first.focus();
		}
	}
</script>

<div
	bind:this={dialogEl}
	class="fixed inset-0 z-50 flex items-center justify-center {fullscreen ? 'p-0' : 'p-4'}"
	role="dialog"
	aria-modal="true"
	aria-labelledby="banked-resets-title"
	tabindex="-1"
	onkeydown={handleDialogKeydown}
>
	<button
		type="button"
		class="absolute inset-0 bg-background/80 backdrop-blur-sm"
		aria-label="Close banked resets"
		tabindex="-1"
		disabled={consumeActive}
		onclick={requestClose}
	></button>

	<div
		bind:this={cardEl}
		class="relative flex flex-col bg-card border border-border shadow-xl {fullscreen
			? 'w-full h-full max-w-none rounded-none'
			: 'w-full max-w-lg max-h-[90vh] rounded-xl'}"
	>
		<header class="flex items-center gap-3 px-6 py-4 border-b border-border">
			<div class="flex-1 min-w-0">
				<h2 id="banked-resets-title" class="text-lg font-semibold text-foreground">Banked resets</h2>
				<p class="mt-0.5 text-xs text-muted-foreground truncate">{provider.name}</p>
			</div>
			<button
				type="button"
				onclick={requestClose}
				disabled={consumeActive}
				class="p-3 -mr-3 hover:bg-muted rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
				aria-label="Close"
			>
				<X class="w-5 h-5 text-muted-foreground" />
			</button>
		</header>

		<div class="flex-1 overflow-y-auto p-6">
			<div class="mb-4 flex items-center justify-between gap-3">
				<p class="text-sm text-muted-foreground">
					{#if hasLoaded}
						{availableCount} {availableCount === 1 ? 'reset' : 'resets'} available
					{:else}
						View and use reset credits saved to this ChatGPT account.
					{/if}
				</p>
				<button
					type="button"
					class="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
					onclick={refreshInventory}
					disabled={consumeActive || loading || refreshing}
				>
					<RefreshCw />{refreshing ? 'Refreshing…' : 'Refresh'}
				</button>
			</div>

			<p class="sr-only" aria-live="polite">{liveMessage}</p>

			{#if inventoryError}
				<div class="mb-4 rounded-lg border border-destructive/40 bg-destructive/5 p-3" role="alert">
					<p class="text-sm text-destructive">{inventoryError}</p>
					<button
						type="button"
						class="mt-3 inline-flex min-h-11 items-center rounded-lg border border-border px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
						onclick={refreshInventory}
						disabled={consumeActive || loading || refreshing}
					>Try again</button>
				</div>
			{/if}

			{#if outcomeMessage}
				<div class="mb-4 rounded-lg border border-border bg-muted p-3" aria-live="polite">
					<p class="text-sm text-foreground">{outcomeMessage}</p>
				</div>
			{/if}

			{#if loading && !hasLoaded}
				<div class="space-y-3" aria-label="Loading banked resets">
					{#each [0, 1] as item (item)}
						<div class="h-24 rounded-lg border border-border bg-muted"></div>
					{/each}
				</div>
			{:else if hasLoaded && credits.length === 0}
				<div class="rounded-lg border border-border bg-muted p-5 text-center">
					<h3 class="text-sm font-semibold text-foreground">No banked resets</h3>
					<p class="mt-1 text-sm text-muted-foreground">New reset credits will appear here when they are granted.</p>
				</div>
			{:else if hasLoaded}
				{#if availableCount > detailedAvailableCount}
					<p class="mb-3 text-xs text-muted-foreground">
						{detailedAvailableCount} of {availableCount} available reset details are shown.
					</p>
				{/if}

				<ul class="space-y-3">
					{#each credits as credit (credit.id)}
						{@const granted = formatUnixSeconds(credit.granted_at)}
						{@const expires = formatUnixSeconds(credit.expires_at)}
						<li class="flex gap-3 rounded-lg border border-border p-4">
							<div class="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-border bg-muted text-muted-foreground" aria-hidden="true">
								<RotateCcw class="h-4 w-4" />
							</div>
							<div class="min-w-0 flex-1">
								<div class="flex items-start justify-between gap-3">
									<div class="min-w-0">
										<h3 class="text-sm font-semibold text-foreground">{creditTitle(credit)}</h3>
										{#if credit.description}
											<p class="mt-1 text-sm text-muted-foreground">{credit.description}</p>
										{/if}
									</div>
									<span class="shrink-0 text-xs font-medium text-muted-foreground">{statusLabel(credit)}</span>
								</div>

								<div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
									<span>{granted ? `Granted ${granted}` : 'Grant date unavailable'}</span>
									<span>{expires ? `Expires ${expires}` : 'No expiration shown'}</span>
								</div>

								{#if canUse(credit)}
									{#if armedCreditId === credit.id}
										<div class="mt-3 rounded-lg border border-border bg-muted p-3">
											<p class="text-sm text-foreground">Eligible Codex windows may reset. This action cannot be undone.</p>
											{#if consumeError}
												<p class="mt-2 text-sm text-destructive" role="alert">{consumeError}</p>
											{/if}
											<div class="mt-3 flex flex-wrap gap-2">
												<button
													type="button"
													class="inline-flex min-h-11 items-center rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
													onclick={() => consumeCredit(credit.id)}
													disabled={consumeActive}
												>
													{consumingCreditId === credit.id ? 'Using…' : consumeError ? 'Try again' : 'Use reset'}
												</button>
												<button
													type="button"
													class="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
													onclick={() => cancelCredit(credit.id)}
													disabled={consumeActive}
												>Cancel</button>
											</div>
										</div>
									{:else}
										<button
											type="button"
											class="mt-3 inline-flex min-h-11 items-center rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
											onclick={() => armCredit(credit.id)}
											disabled={consumeActive}
										>Use</button>
									{/if}
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	</div>
</div>
