<script lang="ts">
	import { pwa, hasUpdate } from '$lib/stores/pwa';

	let isUpdating = false;

	async function handleUpdate() {
		isUpdating = true;
		await pwa.applyUpdate();
		// Page will reload after update is applied
	}

	function handleDismiss() {
		pwa.dismissUpdate();
	}
</script>

{#if $hasUpdate}
	<div
		class="fixed top-4 left-4 right-4 md:left-auto md:right-4 md:w-96 z-50
			   bg-[var(--color-bg-secondary)] border border-[var(--color-primary)]
			   rounded-lg shadow-lg p-4"
		role="alert"
		aria-live="polite"
	>
		<div class="flex items-start gap-3">
			<!-- Update Icon -->
			<div class="flex-shrink-0 w-10 h-10 rounded-full bg-[var(--color-primary)]/10 flex items-center justify-center">
				<svg class="w-5 h-5 text-[var(--color-primary)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
						d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
				</svg>
			</div>

			<!-- Content -->
			<div class="flex-1 min-w-0">
				<h3 class="text-sm font-semibold text-[var(--color-text-primary)]">
					Update Available
				</h3>
				<p class="text-xs text-[var(--color-text-secondary)] mt-1">
					A new version of AI Hub is available. Refresh to update.
				</p>
			</div>

			<!-- Close button -->
			<button
				onclick={handleDismiss}
				class="flex-shrink-0 p-1 text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] transition-colors"
				aria-label="Dismiss"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Actions -->
		<div class="flex gap-2 mt-4">
			<button
				onclick={handleDismiss}
				class="flex-1 px-4 py-2 text-sm font-medium text-[var(--color-text-secondary)]
					   bg-[var(--color-bg-primary)] hover:bg-[var(--color-bg-tertiary)]
					   rounded-lg transition-colors min-h-[44px]"
			>
				Later
			</button>
			<button
				onclick={handleUpdate}
				disabled={isUpdating}
				class="flex-1 px-4 py-2 text-sm font-medium text-white
					   bg-[var(--color-primary)] hover:opacity-90 disabled:opacity-50
					   rounded-lg transition-opacity min-h-[44px] flex items-center justify-center gap-2"
			>
				{#if isUpdating}
					<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
					Updating...
				{:else}
					Refresh
				{/if}
			</button>
		</div>
	</div>
{/if}
