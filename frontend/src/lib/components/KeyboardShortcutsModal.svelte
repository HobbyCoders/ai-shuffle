<script lang="ts">
	import { getShortcutsByCategory, formatShortcut, type KeyboardShortcut } from '$lib/services/keyboard';
	import UniversalModal from './UniversalModal.svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
	}

	let { open, onClose }: Props = $props();

	// Get shortcuts grouped by category
	let shortcutsByCategory = $derived(open ? getShortcutsByCategory() : {});

	// Category display names
	const categoryNames: Record<string, string> = {
		navigation: 'Navigation',
		editing: 'Editing',
		chat: 'Chat',
		general: 'General'
	};

	// Category order
	const categoryOrder = ['chat', 'navigation', 'general', 'editing'];
</script>

{#if open}
	<UniversalModal
		{open}
		title="Keyboard Shortcuts"
		icon="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
		{onClose}
		showFooter={false}
		size="md"
	>
		<div class="space-y-6">
			{#each categoryOrder as categoryKey}
				{@const shortcuts = shortcutsByCategory[categoryKey] || []}
				{#if shortcuts.length > 0}
					<div>
						<h3 class="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-3">
							{categoryNames[categoryKey] || categoryKey}
						</h3>
						<div class="space-y-2">
							{#each shortcuts as shortcut}
								<div class="flex items-center justify-between py-2 px-3 rounded-lg bg-muted/30">
									<span class="text-sm text-foreground">{shortcut.description}</span>
									<kbd class="px-2 py-1 text-xs font-mono bg-muted border border-border rounded text-muted-foreground">
										{formatShortcut(shortcut)}
									</kbd>
								</div>
							{/each}
						</div>
					</div>
				{/if}
			{/each}

			{#if Object.values(shortcutsByCategory).every(arr => arr.length === 0)}
				<div class="text-center py-8 text-muted-foreground">
					<p>No keyboard shortcuts are currently registered.</p>
				</div>
			{/if}

			<!-- Tip at the bottom -->
			<div class="pt-4 border-t border-border">
				<p class="text-xs text-muted-foreground text-center">
					Press <kbd class="px-1.5 py-0.5 text-xs font-mono bg-muted border border-border rounded">Esc</kbd> to close this dialog
				</p>
			</div>
		</div>
	</UniversalModal>
{/if}
