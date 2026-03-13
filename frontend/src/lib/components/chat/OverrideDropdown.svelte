<script lang="ts">
	/**
	 * OverrideDropdown - Reusable dropdown for session override settings
	 * (Thinking, Effort, Permission). Each has a button with icon + active indicator
	 * and a dropdown menu with mode options.
	 */
	import { onMount } from 'svelte';
	import type { Snippet } from 'svelte';

	interface OverrideMode {
		value: string | null;
		label: string;
		description: string;
	}

	interface Props {
		modes: OverrideMode[];
		currentValue: string | null;
		onSelect: (value: string | null) => void;
		icon: Snippet;
		title: string;
	}

	let { modes, currentValue, onSelect, icon, title }: Props = $props();

	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);

	function handleClick(e: MouseEvent) {
		e.stopPropagation();
		showDropdown = !showDropdown;
	}

	function handleSelect(value: string | null) {
		onSelect(value);
		showDropdown = false;
	}

	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showDropdown = false;
		}
	}

	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			showDropdown = false;
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
		};
	});
</script>

<div class="override-wrapper" bind:this={dropdownRef}>
	<button
		type="button"
		class="override-btn"
		class:active={currentValue !== null}
		onclick={handleClick}
		title="{title} (session only)"
		aria-haspopup="menu"
		aria-expanded={showDropdown}
	>
		{@render icon()}
		{#if currentValue !== null}
			<span class="override-indicator"></span>
		{/if}
	</button>

	{#if showDropdown}
		<div class="override-dropdown" role="menu" aria-label={title}>
			<div class="override-dropdown-header">{title}</div>
			{#each modes as mode (mode.value)}
				<button
					type="button"
					class="override-dropdown-item"
					class:selected={currentValue === mode.value}
					onclick={() => handleSelect(mode.value)}
					role="menuitem"
				>
					<span class="override-item-label">{mode.label}</span>
					{#if currentValue === mode.value}
						<svg class="w-3.5 h-3.5 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{/if}
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	/* ===== OVERRIDE BUTTONS ===== */
	.override-wrapper {
		position: relative;
	}

	.override-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.override-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.override-btn.active {
		background: var(--selection-bg);
		color: var(--selection-text);
	}

	.override-btn.active:hover {
		background: var(--selection-bg-emphasis);
	}

	/* Active indicator dot */
	.override-indicator {
		position: absolute;
		top: 2px;
		right: 2px;
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
		border: 1px solid var(--background);
	}

	/* Override dropdown */
	.override-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		right: 0;
		min-width: 140px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		z-index: 50;
		padding: 4px;
		animation: override-dropdown-fade-in 0.15s ease;
	}

	@keyframes override-dropdown-fade-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.override-dropdown-header {
		padding: 6px 10px 4px;
		font-size: 10px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.override-dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		font-size: 13px;
		color: var(--popover-foreground);
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		transition: background-color 0.15s;
		text-align: left;
	}

	.override-dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.override-dropdown-item.selected {
		background: var(--selection-bg);
	}

	.override-item-label {
		font-weight: 500;
		flex: 1;
	}

	.override-dropdown-item .check-icon {
		flex-shrink: 0;
		color: var(--primary);
		margin-left: auto;
	}

	/* Mobile: slightly more compact */
	@media (max-width: 639px) {
		.override-btn {
			width: 26px;
			height: 26px;
		}

		.override-btn :global(svg) {
			width: 14px;
			height: 14px;
		}

		.override-dropdown {
			min-width: 120px;
		}
	}
</style>
