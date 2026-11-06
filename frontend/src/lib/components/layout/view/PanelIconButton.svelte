<script lang="ts">
	import type { SettingsIcon } from '$lib/settings/categories';
	let { label, icon: Icon, onclick, disabled = false, loading = false }: {
		label: string; icon: SettingsIcon; onclick: () => void; disabled?: boolean; loading?: boolean;
	} = $props();
</script>
<button type="button" class:loading aria-label={label} title={label} aria-busy={loading || undefined}
	disabled={disabled || loading} {onclick}><Icon size={18} strokeWidth={1.75} aria-hidden="true" /></button>
<style>
	button { display: grid; place-items: center; flex: none; width: 36px; height: 36px; padding: 0; border: 0; border-radius: var(--radius-control); background: transparent; color: var(--sidebar-muted); cursor: pointer; transition: background 0.2s ease-out, color 0.2s ease-out; }
	button:hover:not(:disabled) { background: var(--sidebar-accent); color: var(--foreground); }
	button:active:not(:disabled) { background: var(--row-selected); }
	button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	button:disabled { opacity: 0.45; cursor: default; }
	.loading :global(svg) { animation: panel-action-pulse 1s ease-in-out infinite alternate; }
	@keyframes panel-action-pulse { to { opacity: 0.3; } }
	@media (pointer: coarse), (max-width: 600px) { button { width: 44px; height: 44px; } }
	@media (prefers-reduced-motion: reduce) { button { transition: none; } .loading :global(svg) { animation: none; } }
</style>
