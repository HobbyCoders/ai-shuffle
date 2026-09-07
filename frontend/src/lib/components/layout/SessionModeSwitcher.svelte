<script lang="ts">
	/**
	 * SessionModeSwitcher — the sidebar's lane picker (Code / Chat / Voice).
	 *
	 * Sits directly under the brand row and decides which lane the whole panel
	 * is looking at: the session list, the filters and the composer all follow
	 * it. Because it changes what the list contains rather than what a single
	 * control does, it is a radiogroup — three mutually exclusive views of the
	 * same panel — with roving-tabindex arrow keys, so a keyboard user tabs
	 * into the group once and cycles lanes with the arrows.
	 *
	 * It rides in the sidebar's bottom dock, so it is styled as a segmented
	 * control: an inset trough with ONE raised thumb that slides between the
	 * three equal columns. The thumb is a sibling element rather than a
	 * background on the selected button, which is the only way the change of
	 * lane reads as a movement instead of two independent fades — and it is a
	 * transform, so the slide never touches layout. Sized so all three labels
	 * survive the panel's 220px minimum; below ~250px of switcher width
	 * the labels drop and the icons carry the meaning — which is why each radio
	 * carries an explicit `aria-label`: `display: none` takes the visible label
	 * out of the accessibility tree too, so without it the narrow sidebar would
	 * present three unnamed radios.
	 */
	import { AudioLines, Code2, MessagesSquare } from 'lucide-svelte';
	import { SESSION_MODES, type SessionMode } from '$lib/stores/sessionMode.svelte';

	interface Props {
		value: SessionMode;
		onSelect: (mode: SessionMode) => void;
	}

	let { value, onSelect }: Props = $props();

	const OPTIONS: { mode: SessionMode; label: string; icon: typeof Code2 }[] = [
		{ mode: 'code', label: 'Code', icon: Code2 },
		{ mode: 'chat', label: 'Chat', icon: MessagesSquare },
		{ mode: 'voice', label: 'Voice', icon: AudioLines }
	];

	let buttons = $state<(HTMLButtonElement | null)[]>([]);

	/** Which column the thumb sits over. Clamped so an unknown value parks it
	 *  on the first lane instead of sliding the thumb out of the trough. */
	const index = $derived(Math.max(0, OPTIONS.findIndex((o) => o.mode === value)));

	/** Arrow keys move the selection AND the focus, which is what a radiogroup
	 *  promises — Home/End jump to the ends. */
	function handleKeydown(e: KeyboardEvent, index: number) {
		let next: number;
		switch (e.key) {
			case 'ArrowRight':
			case 'ArrowDown':
				next = (index + 1) % SESSION_MODES.length;
				break;
			case 'ArrowLeft':
			case 'ArrowUp':
				next = (index - 1 + SESSION_MODES.length) % SESSION_MODES.length;
				break;
			case 'Home':
				next = 0;
				break;
			case 'End':
				next = SESSION_MODES.length - 1;
				break;
			default:
				return;
		}
		e.preventDefault();
		buttons[next]?.focus();
		onSelect(OPTIONS[next].mode);
	}
</script>

<div class="modes" role="radiogroup" aria-label="Session mode" style="--mode-index:{index}">
	<span class="thumb" aria-hidden="true"></span>
	{#each OPTIONS as opt, i (opt.mode)}
		<button
			bind:this={buttons[i]}
			class="mode"
			class:on={value === opt.mode}
			type="button"
			role="radio"
			aria-checked={value === opt.mode}
			aria-label={opt.label}
			tabindex={value === opt.mode ? 0 : -1}
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
		grid-template-columns: repeat(3, 1fr);
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
		width: calc((100% - 6px) / 3);
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
		height: 28px;
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
		.mode { height: 40px; }
	}

	:global(html[data-platform='mobile']) .mode { height: 40px; }

	@media (prefers-reduced-motion: reduce) {
		.mode, .thumb { transition: none; }
	}
</style>
