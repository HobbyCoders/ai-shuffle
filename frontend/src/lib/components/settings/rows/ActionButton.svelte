<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HTMLButtonAttributes } from 'svelte/elements';

	/** The Settings action treatment for dialogs also rendered outside SettingsShell. */
	let { variant = 'default', icon = false, children, ...props }: HTMLButtonAttributes & {
		variant?: 'default' | 'primary' | 'ghost' | 'danger';
		icon?: boolean;
		children?: Snippet;
	} = $props();
</script>

<button type="button" {...props} class="st-action" class:primary={variant === 'primary'}
	class:ghost={variant === 'ghost'} class:danger={variant === 'danger'} class:icon>
	{@render children?.()}
</button>

<style>
	.st-action {
		display: inline-flex; align-items: center; justify-content: center; gap: 6px;
		height: 32px; padding: 0 12px; flex-shrink: 0;
		border-radius: var(--st-radius-md, var(--radius));
		border: 1px solid var(--st-border-strong, var(--border));
		background: transparent; color: var(--st-fg, var(--foreground));
		font: 500 13px var(--font-sans); white-space: nowrap; cursor: pointer;
		transition: background 150ms, border-color 150ms;
	}
	.st-action:hover:not(:disabled) { background: var(--st-hover, var(--hover-overlay)); }
	.st-action.primary { background: var(--st-action, var(--action)); color: var(--st-action-fg, var(--action-foreground)); border-color: transparent; }
	.st-action.primary:hover:not(:disabled) { background: var(--st-action-hover, var(--action-hover)); }
	.st-action.ghost { border-color: transparent; }
	.st-action.danger { color: var(--st-destructive, var(--destructive)); border-color: var(--st-destructive-line, var(--border)); }
	.st-action.icon { width: 32px; padding: 0; }
	.st-action :global(svg) { width: 15px; height: 15px; flex-shrink: 0; }
	.st-action:focus-visible { outline: 2px solid var(--st-primary, var(--ring)); outline-offset: 2px; }
	.st-action:active:not(:disabled) { transform: translateY(1px); }
	.st-action:disabled { opacity: .45; cursor: not-allowed; }
	@media (max-width: 600px) { .st-action { min-height: 44px; } .st-action.icon { width: 44px; } }
	@media (prefers-reduced-motion: reduce) { .st-action { transition: none; } }
</style>
