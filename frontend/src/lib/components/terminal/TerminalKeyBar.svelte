<script lang="ts">
	/**
	 * Keys a phone keyboard lacks. Buttons never take focus (pointerdown is
	 * prevented), so the on-screen keyboard stays up while you tap them.
	 */
	let { ctrl, disabled = false, onKey, onCtrl }: {
		ctrl: boolean;
		disabled?: boolean;
		onKey: (data: string) => void;
		onCtrl: () => void;
	} = $props();

	const KEYS: { label: string; data: string; aria: string }[] = [
		{ label: 'Esc', data: '\x1b', aria: 'Escape' },
		{ label: 'Tab', data: '\t', aria: 'Tab' },
		{ label: '^C', data: '\x03', aria: 'Control C' },
		{ label: '↑', data: '\x1b[A', aria: 'Up arrow' },
		{ label: '↓', data: '\x1b[B', aria: 'Down arrow' },
		{ label: '←', data: '\x1b[D', aria: 'Left arrow' },
		{ label: '→', data: '\x1b[C', aria: 'Right arrow' },
		{ label: '|', data: '|', aria: 'Pipe' },
		{ label: '~', data: '~', aria: 'Tilde' },
		{ label: '/', data: '/', aria: 'Slash' },
		{ label: '-', data: '-', aria: 'Dash' },
	];
</script>

<div class="keybar" role="toolbar" aria-label="Terminal keys">
	<button type="button" class="key mod" aria-pressed={ctrl} {disabled}
		onpointerdown={(e) => e.preventDefault()} onclick={onCtrl}>Ctrl</button>
	{#each KEYS as key (key.label)}
		<button type="button" class="key" aria-label={key.aria} {disabled}
			onpointerdown={(e) => e.preventDefault()} onclick={() => onKey(key.data)}>{key.label}</button>
	{/each}
</div>

<style>
	.keybar {
		display: flex; gap: 4px; padding: 6px 8px; overflow-x: auto; flex-shrink: 0;
		border-top: 1px solid var(--border); background: var(--muted); scrollbar-width: none;
	}
	.keybar::-webkit-scrollbar { display: none; }
	.key {
		flex: 0 0 auto; min-width: 44px; height: 40px; padding: 0 10px;
		border: 1px solid var(--border-strong); border-bottom-width: 2px; border-radius: 7px;
		background: var(--background); color: var(--foreground); font: 500 13px var(--font-mono);
		touch-action: manipulation; cursor: pointer; transition: background-color 0.12s, transform 0.08s;
	}
	.key:active:not(:disabled) { transform: translateY(1px); border-bottom-width: 1px; }
	.key:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.key:disabled { opacity: 0.4; cursor: not-allowed; }
	.mod[aria-pressed='true'] { background: var(--primary); border-color: var(--primary); color: var(--primary-foreground); }
	@media (prefers-reduced-motion: reduce) { .key { transition: none; } }
</style>
