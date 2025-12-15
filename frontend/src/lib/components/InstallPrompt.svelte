<script lang="ts">
	import { pwa, isInstallable } from '$lib/stores/pwa';

	let showPrompt = false;
	let isDismissed = false;

	// Show the prompt with a delay to not be intrusive
	$: if ($isInstallable && !isDismissed) {
		setTimeout(() => {
			showPrompt = true;
		}, 3000);
	} else {
		showPrompt = false;
	}

	async function handleInstall() {
		const installed = await pwa.promptInstall();
		if (installed) {
			showPrompt = false;
		}
	}

	function handleDismiss() {
		isDismissed = true;
		showPrompt = false;
	}
</script>

{#if showPrompt}
	<div
		class="fixed bottom-4 left-4 right-4 md:left-auto md:right-4 md:w-96 z-50
			   bg-[var(--color-bg-secondary)] border border-[var(--color-border)]
			   rounded-lg shadow-lg p-4 animate-slide-up"
		role="dialog"
		aria-label="Install AI Hub"
	>
		<div class="flex items-start gap-3">
			<!-- App Icon -->
			<div class="flex-shrink-0 w-12 h-12 rounded-lg bg-[var(--color-bg-primary)] flex items-center justify-center">
				<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" class="w-8 h-8">
					<rect width="100" height="100" rx="20" fill="#1a1a1a"/>
					<circle cx="50" cy="50" r="35" fill="none" stroke="#e5604d" stroke-width="6"/>
					<circle cx="50" cy="50" r="15" fill="#e5604d"/>
				</svg>
			</div>

			<!-- Content -->
			<div class="flex-1 min-w-0">
				<h3 class="text-sm font-semibold text-[var(--color-text-primary)]">
					Install AI Hub
				</h3>
				<p class="text-xs text-[var(--color-text-secondary)] mt-1">
					Add to your home screen for quick access and offline support.
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
				Not now
			</button>
			<button
				onclick={handleInstall}
				class="flex-1 px-4 py-2 text-sm font-medium text-white
					   bg-[var(--color-primary)] hover:opacity-90
					   rounded-lg transition-opacity min-h-[44px]"
			>
				Install
			</button>
		</div>
	</div>
{/if}

<style>
	@keyframes slide-up {
		from {
			transform: translateY(100%);
			opacity: 0;
		}
		to {
			transform: translateY(0);
			opacity: 1;
		}
	}

	.animate-slide-up {
		animation: slide-up 0.3s ease-out;
	}
</style>
