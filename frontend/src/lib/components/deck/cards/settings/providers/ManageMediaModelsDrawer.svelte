<script lang="ts">
	/**
	 * ManageMediaModelsDrawer — the "Manage media models" drawer (Chunk 8, ruling D5).
	 *
	 * A thin container that hosts `AIToolsSettings` UNCHANGED (plan §2.1:
	 * "AIToolsSettings + ToolCategoryCard + ModelPickerGrid + ModelBrowser move
	 * unchanged in behavior"). Opened from MediaToolsSection's "AI media models"
	 * row. AIToolsSettings persists its own state, so there is no save/cancel
	 * footer — the drawer is purely a close affordance around it.
	 *
	 * Drawer chrome: role="dialog", aria-modal,
	 * right-side drawer on the desktop shell (clamped to the viewport at every
	 * width) / fullscreen on the mobile shell, backdrop-click + Esc + ✕ to close, self-contained
	 * Tailwind theme-token utilities. Wide (max-w-3xl) to host the full
	 * media-model browser grid.
	 */
	import { onMount } from 'svelte';
	import { X } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import AIToolsSettings from '../AIToolsSettings.svelte';

	interface Props {
		onClose: () => void;
	}
	let { onClose }: Props = $props();

	// ----- Fullscreen on the mobile shell (mirrors AddProviderModal) -----
	const fullscreen = $derived(platform.isMobile);

	// Esc is handled via keydown on the dialog container, which only fires while
	// focus is inside it — so focus it on mount.
	let dialogEl: HTMLDivElement;

	onMount(() => {
		dialogEl?.focus();
	});

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') onClose();
	}
</script>

<div
	bind:this={dialogEl}
	class="fixed inset-0 z-50 flex justify-end"
	role="dialog"
	aria-modal="true"
	aria-label="Manage media models"
	onkeydown={handleKeydown}
	tabindex="-1"
>
	<!-- Backdrop -->
	<button
		class="absolute inset-0 bg-background/80 backdrop-blur-xs"
		onclick={onClose}
		aria-label="Close"
	></button>

	<!-- Drawer panel -->
	<div
		class="relative flex h-full flex-col bg-card shadow-xl {fullscreen
			? 'w-full max-w-none'
			: 'w-full max-w-3xl border-l border-border'}"
	>
		<!-- Header -->
		<div class="flex items-start gap-2 px-6 py-4 border-b border-border">
			<div class="min-w-0 flex-1">
				<h2 class="truncate text-lg font-semibold text-foreground">AI media models</h2>
				<p class="text-xs text-muted-foreground mt-0.5">Image, video, audio and 3D generation models</p>
			</div>
			<button
				type="button"
				onclick={onClose}
				class="p-2 -mr-2 hover:bg-muted rounded-lg transition-colors"
				aria-label="Close"
			>
				<X class="w-5 h-5 text-muted-foreground" />
			</button>
		</div>

		<!-- Body -->
		<div class="flex-1 overflow-y-auto p-6">
			<AIToolsSettings />
		</div>
	</div>
</div>
