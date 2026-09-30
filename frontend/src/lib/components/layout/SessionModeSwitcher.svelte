<script lang="ts">
	/** Select the capabilities for a new conversation. History is never partitioned. */
	import { Code2, MessagesSquare } from 'lucide-svelte';
	import { type SessionMode } from '$lib/stores/sessionMode.svelte';

	interface Props {
		value: SessionMode;
		onSelect: (mode: SessionMode) => void;
	}

	let { value, onSelect }: Props = $props();

	const OPTIONS: { mode: SessionMode; label: string; icon: typeof Code2 }[] = [
		{ mode: 'code', label: 'Workspace', icon: Code2 },
		{ mode: 'chat', label: 'Chat', icon: MessagesSquare }
	];

	let buttons = $state<(HTMLButtonElement | null)[]>([]);

	/** Which column the thumb sits over. Clamped so an unknown value parks it
	 *  on the first lane instead of sliding the thumb out of the trough. */
	const index = $derived(Math.max(0, OPTIONS.findIndex((o) => o.mode === (value === 'voice' ? 'chat' : value))));

	/** Arrow keys move the selection AND the focus, which is what a radiogroup
	 *  promises — Home/End jump to the ends. */
	function handleKeydown(e: KeyboardEvent, index: number) {
		let next: number;
		switch (e.key) {
			case 'ArrowRight':
			case 'ArrowDown':
				next = (index + 1) % OPTIONS.length;
				break;
			case 'ArrowLeft':
			case 'ArrowUp':
				next = (index - 1 + OPTIONS.length) % OPTIONS.length;
				break;
			case 'Home':
				next = 0;
				break;
			case 'End':
				next = OPTIONS.length - 1;
				break;
			default:
				return;
		}
		e.preventDefault();
		buttons[next]?.focus();
		onSelect(OPTIONS[next].mode);
	}
</script>

<div class="modes" role="radiogroup" aria-label="New conversation capabilities" style="--mode-index:{index}">
	<span class="thumb" aria-hidden="true"></span>
	{#each OPTIONS as opt, i (opt.mode)}
		<button
			bind:this={buttons[i]}
			class="mode"
			class:on={index === i}
			type="button"
			role="radio"
			aria-checked={index === i}
			aria-label={opt.label}
			tabindex={index === i ? 0 : -1}
			onclick={() => onSelect(opt.mode)}
			onkeydown={(e) => handleKeydown(e, i)}
		>
			<opt.icon size={14} strokeWidth={1.9} />
			<span class="label">{opt.label}</span>
		</button>
	{/each}
</div>

<style>
	.modes {
		position: relative;
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		margin: 0;
		padding: 3px;
		border: 1px solid var(--sidebar-border);
		border-radius: 9px;
		background: oklch(0 0 0 / 0.5);
		box-shadow: inset 0 1px 2px oklch(0 0 0 / 0.6);
		container-type: inline-size;
	}
	.thumb {
		position: absolute;
		top: 3px;
		bottom: 3px;
		left: 3px;
		width: calc((100% - 6px) / 2);
		border-radius: 7px;
		background: linear-gradient(
			180deg,
			color-mix(in oklch, var(--sidebar-accent-active) 100%, var(--foreground) 6%),
			var(--sidebar-accent)
		);
		box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.1), 0 2px 6px oklch(0 0 0 / 0.5);
		transform: translateX(calc(var(--mode-index, 0) * 100%));
		transition: transform 0.26s cubic-bezier(0.16, 1, 0.3, 1);
		pointer-events: none;
	}
	.mode {
		position: relative;
		z-index: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		height: 36px;
		padding: 0 6px;
		border: none;
		background: transparent;
		border-radius: 7px;
		color: var(--sidebar-muted);
		font-size: 11.5px;
		font-weight: 500;
		letter-spacing: 0.01em;
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.mode :global(svg) { flex: none; color: currentColor; }
	.mode .label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mode:hover { color: var(--foreground); }
	/* The thumb carries the selection; the button only brightens. */
	.mode.on { color: var(--foreground); }
	.mode.on :global(svg) { color: var(--primary); }

	/* The sidebar is user-resizable down to 220px; at that width three labels
	   plus their icons stop fitting, so the icons carry the lane on their own. */
	@container (max-width: 250px) {
		.mode .label { display: none; }
		.mode { gap: 0; }
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* Touch sizing: the mobile shell, plus any touch-only pointer. */
	@media (hover: none) {
		.mode { height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.mode, .thumb { transition: none; }
	}
</style>