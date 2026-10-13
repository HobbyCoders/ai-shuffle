<script lang="ts">
	/** Pick what a NEW conversation can do: a Workspace (code tools) or a plain
	 *  Chat. Icon-only, so it fits the sidebar header beside the bell; the label
	 *  rides on aria-label and the hover tooltip. History is never partitioned. */
	import { Code2, MessagesSquare } from 'lucide-svelte';
	import { type SessionMode } from '$lib/stores/sessionMode.svelte';

	interface Props {
		value: SessionMode;
		onSelect: (mode: SessionMode) => void;
	}

	let { value, onSelect }: Props = $props();

	const OPTIONS: { mode: SessionMode; label: string; hint: string; icon: typeof Code2 }[] = [
		{ mode: 'code', label: 'Workspace', hint: 'New sessions get workspace tools', icon: Code2 },
		{ mode: 'chat', label: 'Chat', hint: 'New sessions are plain chats', icon: MessagesSquare }
	];

	let buttons = $state<(HTMLButtonElement | null)[]>([]);

	/** Which tab the thumb sits under. Clamped so an unknown value parks it on
	 *  the first tab; a historical voice session reads as Chat. */
	const index = $derived(Math.max(0, OPTIONS.findIndex((o) => o.mode === (value === 'voice' ? 'chat' : value))));

	/** Arrow keys move the selection AND the focus, which is what a radiogroup
	 *  promises; Home/End jump to the ends. */
	function handleKeydown(e: KeyboardEvent, i: number) {
		let next: number;
		switch (e.key) {
			case 'ArrowRight':
			case 'ArrowDown':
				next = (i + 1) % OPTIONS.length;
				break;
			case 'ArrowLeft':
			case 'ArrowUp':
				next = (i - 1 + OPTIONS.length) % OPTIONS.length;
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

<div class="modetog" role="radiogroup" aria-label="New conversation capabilities" style="--i:{index}">
	<span class="thumb" aria-hidden="true"></span>
	{#each OPTIONS as opt, i (opt.mode)}
		<button
			bind:this={buttons[i]}
			class="tab"
			class:on={index === i}
			type="button"
			role="radio"
			aria-checked={index === i}
			aria-label={opt.label}
			title={`${opt.label}: ${opt.hint.toLowerCase()}`}
			tabindex={index === i ? 0 : -1}
			onclick={() => onSelect(opt.mode)}
			onkeydown={(e) => handleKeydown(e, i)}
		>
			<opt.icon size={15} strokeWidth={1.9} />
		</button>
	{/each}
</div>

<style>
	.modetog {
		position: relative;
		display: inline-grid;
		grid-template-columns: repeat(2, 34px);
		flex: none;
		padding: 3px;
		border: 1px solid var(--sidebar-border);
		border-radius: 10px;
		background: oklch(1 0 0 / 0.035);
	}
	.thumb {
		position: absolute;
		top: 3px;
		bottom: 3px;
		left: 3px;
		width: 34px;
		border-radius: 7px;
		background: var(--sidebar-accent-active);
		box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.08), 0 1px 4px oklch(0 0 0 / 0.45);
		transform: translateX(calc(var(--i, 0) * 100%));
		transition: transform 0.24s cubic-bezier(0.16, 1, 0.3, 1);
		pointer-events: none;
	}
	.tab {
		position: relative;
		z-index: 1;
		display: grid;
		place-items: center;
		height: 26px;
		padding: 0;
		border: none;
		border-radius: 7px;
		background: transparent;
		color: var(--sidebar-muted);
		cursor: pointer;
		transition: color 0.13s ease;
	}
	.tab:hover { color: var(--foreground); }
	.tab.on { color: var(--primary); }
	.tab:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* Touch sizing: the mobile shell, plus any touch-only pointer. */
	@media (hover: none) {
		.modetog { grid-template-columns: repeat(2, 40px); border-radius: 11px; }
		.thumb { width: 40px; border-radius: 8px; }
		.tab { height: 32px; border-radius: 8px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.tab, .thumb { transition: none; }
	}
</style>
