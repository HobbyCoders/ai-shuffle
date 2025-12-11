<script lang="ts">
	/**
	 * UniversalModal - A universal modal component with tabbed interface
	 *
	 * Design inspired by modern settings dialogs with:
	 * - Header with icon and title
	 * - Optional tab navigation
	 * - Two-column layout option for wider content
	 * - Fullscreen on mobile (like sessions panel)
	 * - Footer with cancel/save actions
	 */

	interface Tab {
		id: string;
		label: string;
	}

	interface Props {
		open: boolean;
		title: string;
		icon?: string; // SVG path for the header icon
		tabs?: Tab[];
		activeTab?: string;
		onTabChange?: (tabId: string) => void;
		onClose: () => void;
		onSave?: () => void;
		saveLabel?: string;
		saveDisabled?: boolean;
		saving?: boolean;
		showFooter?: boolean;
		size?: 'sm' | 'md' | 'lg' | 'xl' | 'full';
		twoColumn?: boolean;
	}

	let {
		open,
		title,
		icon,
		tabs = [],
		activeTab = '',
		onTabChange,
		onClose,
		onSave,
		saveLabel = 'Save',
		saveDisabled = false,
		saving = false,
		showFooter = true,
		size = 'lg',
		twoColumn = false
	}: Props = $props();

	// Handle escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			onClose();
		}
	}

	// Size classes mapping
	const sizeClasses = {
		sm: 'max-w-md',
		md: 'max-w-lg',
		lg: 'max-w-2xl',
		xl: 'max-w-4xl',
		full: 'max-w-6xl'
	};
</script>

<svelte:window on:keydown={handleKeydown} />

{#if open}
	<!-- Backdrop with blur -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center"
		role="dialog"
		aria-modal="true"
		aria-labelledby="modal-title"
	>
		<!-- Dark overlay backdrop -->
		<button
			class="absolute inset-0 bg-black/60 backdrop-blur-sm"
			onclick={onClose}
			aria-label="Close modal"
		></button>

		<!-- Modal container - fullscreen on mobile, centered on desktop -->
		<div
			class="
				relative w-full {sizeClasses[size]} mx-auto
				bg-card border border-border rounded-2xl shadow-2xl
				flex flex-col overflow-hidden
				max-h-[90vh]
				sm:max-h-[85vh]

				max-sm:fixed max-sm:inset-3 max-sm:bottom-[4.5rem] max-sm:w-auto max-sm:max-w-none max-sm:max-h-none max-sm:rounded-2xl

				transform transition-all duration-200 ease-out
				animate-modal-in
			"
			onclick={(e) => e.stopPropagation()}
		>
			<!-- Header -->
			<header class="shrink-0 px-6 py-4 border-b border-border bg-card">
				<div class="flex items-center justify-between">
					<div class="flex items-center gap-3">
						<!-- Icon -->
						{#if icon}
							<div class="w-10 h-10 rounded-xl bg-primary/15 flex items-center justify-center">
								<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={icon} />
								</svg>
							</div>
						{/if}
						<h2 id="modal-title" class="text-xl font-semibold text-foreground">{title}</h2>
					</div>

					<!-- Close button -->
					<button
						onclick={onClose}
						class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
						aria-label="Close"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<!-- Tab Navigation -->
				{#if tabs.length > 0}
					<nav class="flex gap-1 mt-4 -mb-4 px-0 overflow-x-auto scrollbar-none" aria-label="Modal tabs">
						{#each tabs as tab}
							<button
								onclick={() => onTabChange?.(tab.id)}
								class="
									px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap
									border-b-2 -mb-[1px]
									{activeTab === tab.id
										? 'text-primary border-primary bg-primary/5'
										: 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}
								"
								aria-current={activeTab === tab.id ? 'page' : undefined}
							>
								{tab.label}
							</button>
						{/each}
					</nav>
				{/if}
			</header>

			<!-- Body - scrollable content area -->
			<div class="flex-1 overflow-y-auto overflow-x-hidden {twoColumn ? 'p-0' : 'p-6'}">
				{#if twoColumn}
					<div class="grid grid-cols-1 md:grid-cols-2 gap-0 md:divide-x divide-border min-h-full">
						<slot name="left" />
						<slot name="right" />
					</div>
				{:else}
					<slot />
				{/if}
			</div>

			<!-- Footer -->
			{#if showFooter}
				<footer class="shrink-0 px-6 py-4 border-t border-border bg-card/50 flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-3">
					<button
						onclick={onClose}
						class="
							px-6 py-2.5 rounded-xl text-sm font-medium
							bg-muted text-foreground border border-border
							hover:bg-accent transition-colors
						"
					>
						Cancel
					</button>

					{#if onSave}
						<button
							onclick={onSave}
							disabled={saveDisabled || saving}
							class="
								px-6 py-2.5 rounded-xl text-sm font-medium
								bg-primary text-primary-foreground
								hover:opacity-90 transition-all
								disabled:opacity-50 disabled:cursor-not-allowed
								flex items-center justify-center gap-2
								min-w-[100px]
							"
						>
							{#if saving}
								<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
							{/if}
							{saveLabel}
						</button>
					{/if}
				</footer>
			{/if}
		</div>
	</div>
{/if}

<style>
	@keyframes modal-in {
		from {
			opacity: 0;
			transform: scale(0.95) translateY(10px);
		}
		to {
			opacity: 1;
			transform: scale(1) translateY(0);
		}
	}

	.animate-modal-in {
		animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.scrollbar-none {
		scrollbar-width: none;
		-ms-overflow-style: none;
	}

	.scrollbar-none::-webkit-scrollbar {
		display: none;
	}
</style>
