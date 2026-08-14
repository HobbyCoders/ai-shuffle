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
		/** Optional section heading. When ANY mode carries one, the list renders
		 *  grouped (headings in first-appearance order); otherwise it stays flat.
		 *  Used by the model override to render the provider-grouped library. */
		group?: string;
		/** Stable bucket identity. Two rows share a group ONLY when this matches —
		 *  provider brand names collide (both OpenAI rows display as "OpenAI"), so
		 *  the caller passes the provider id here to keep the groups separate.
		 *  Defaults to `group` when omitted. */
		groupKey?: string;
		/** Muted qualifier rendered beside the heading (e.g. `API` / `prolite`) —
		 *  what disambiguates two same-brand groups. */
		groupSubtext?: string | null;
	}

	interface Props {
		modes: OverrideMode[];
		currentValue: string | null;
		onSelect: (value: string | null) => void;
		icon: Snippet;
		title: string;
		/** Whether the active/overridden indicator should light up. Defaults to
		 *  `currentValue !== null`. Pass explicitly when `currentValue` reflects an
		 *  effective (possibly profile-inherited) value rather than a real override. */
		isOverridden?: boolean;
		/** Open state is owned by the parent so sibling dropdowns are mutually
		 *  exclusive — opening one closes the others. */
		isOpen: boolean;
		onOpenChange: (open: boolean) => void;
		/** Desktop (>=640px) label rendered beside the icon as a full pill
		 *  (icon + label + chevron). When omitted the control stays icon-only at
		 *  every width. */
		buttonLabel?: string;
		/** Elevates the control to a warning state (amber border/text + warning
		 *  indicator dot). Used to flag a model reroute (actual != requested). */
		warn?: boolean;
		/** Tooltip shown in place of the default one while `warn` is set. */
		warnTitle?: string;
		/** One-line notice rendered at the top of the open dropdown (e.g. the
		 *  reroute explanation). */
		notice?: string;
	}

	let {
		modes,
		currentValue,
		onSelect,
		icon,
		title,
		isOverridden,
		isOpen,
		onOpenChange,
		buttonLabel,
		warn = false,
		warnTitle,
		notice
	}: Props = $props();

	const isActive = $derived(isOverridden ?? currentValue !== null);

	// Grouped rendering: only kicks in when at least one mode declares a group
	// (the effort/permission dropdowns stay byte-identical flat lists). Groups
	// keep first-appearance order so the caller controls the ordering.
	const isGrouped = $derived(modes.some((m) => m.group != null));
	interface ModeGroup {
		key: string;
		group: string;
		subtext: string | null;
		items: OverrideMode[];
	}
	const groupedModes = $derived.by<ModeGroup[]>(() => {
		const out: ModeGroup[] = [];
		for (const m of modes) {
			const name = m.group ?? '';
			// Bucket on the stable key, not the display name — two provider rows
			// can legitimately share a brand (both OpenAI rows) and must NOT merge.
			const key = m.groupKey ?? name;
			let bucket = out.find((g) => g.key === key);
			if (!bucket) {
				bucket = { key, group: name, subtext: m.groupSubtext ?? null, items: [] };
				out.push(bucket);
			}
			bucket.items.push(m);
		}
		return out;
	});

	let dropdownRef = $state<HTMLDivElement | null>(null);

	function handleClick(e: MouseEvent) {
		e.stopPropagation();
		onOpenChange(!isOpen);
	}

	function handleSelect(value: string | null) {
		onSelect(value);
		onOpenChange(false);
	}

	function handleClickOutside(e: MouseEvent) {
		if (isOpen && dropdownRef && !dropdownRef.contains(e.target as Node)) {
			onOpenChange(false);
		}
	}

	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape' && isOpen) {
			onOpenChange(false);
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
		class:active={isActive && !warn}
		class:has-label={buttonLabel != null}
		class:warn
		onclick={handleClick}
		title={warn && warnTitle ? warnTitle : `${title} (session only)`}
		aria-haspopup="menu"
		aria-expanded={isOpen}
	>
		{@render icon()}
		{#if buttonLabel != null}
			<span class="override-label">{buttonLabel}</span>
			<svg class="override-chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 9l6 6 6-6" />
			</svg>
		{/if}
		{#if isActive || warn}
			<span class="override-indicator" class:warn></span>
		{/if}
	</button>

	{#if isOpen}
		<div class="override-dropdown" role="menu" aria-label={title}>
			<div class="override-dropdown-header">{title}</div>
			{#if notice}
				<div class="override-dropdown-notice">{notice}</div>
			{/if}
			{#if isGrouped}
				{#each groupedModes as g (g.key)}
					{#if g.group}
						<div class="override-dropdown-grouphead">
							{g.group}{#if g.subtext}<span class="override-dropdown-groupsub"
									>&nbsp;· {g.subtext}</span
								>{/if}
						</div>
					{/if}
					{#each g.items as mode (mode.value)}
						{@render item(mode)}
					{/each}
				{/each}
			{:else}
				{#each modes as mode (mode.value)}
					{@render item(mode)}
				{/each}
			{/if}
		</div>
	{/if}
</div>

{#snippet item(mode: OverrideMode)}
	<button
		type="button"
		class="override-dropdown-item"
		class:selected={currentValue === mode.value}
		onclick={() => handleSelect(mode.value)}
		role="menuitem"
		title={mode.description || undefined}
	>
		<span class="override-item-label">{mode.label}</span>
		{#if currentValue === mode.value}
			<svg class="w-3.5 h-3.5 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
			</svg>
		{/if}
	</button>
{/snippet}

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

	/* Reroute warning — the actual model differs from what was requested. The
	   component never sets both `.active` and `.warn` at once (see the
	   `class:active={isActive && !warn}` guard above), so these rules are
	   order-independent. Icon-only mode tints the glyph amber; the pill variant
	   below repaints the whole chip. */
	.override-btn.warn,
	.override-btn.warn:hover {
		color: var(--warning);
	}

	/* Label + chevron (desktop pill only — hidden until the 640px breakpoint) */
	.override-label {
		display: none;
		max-width: 92px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		line-height: 1;
		letter-spacing: 0.01em;
	}

	.override-chevron {
		display: none;
		width: 11px;
		height: 11px;
		flex-shrink: 0;
		opacity: 0.6;
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

	.override-indicator.warn {
		background: var(--warning);
	}

	/* One-line reroute notice at the head of the open dropdown. */
	.override-dropdown-notice {
		margin: 0 2px 4px;
		padding: 6px 8px;
		font-size: 11px;
		line-height: 1.3;
		font-weight: 500;
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 40%, transparent);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
	}

	/* Override dropdown */
	.override-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		right: 0;
		min-width: 140px;
		max-width: min(320px, 82vw);
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		z-index: 50;
		padding: 4px;
		animation: override-dropdown-fade-in 0.15s ease;
		/* The model override now lists the whole provider-grouped library (PA13),
		   which can run to dozens of rows — cap it and scroll instead of growing
		   the popup off the top of the viewport. */
		max-height: min(420px, 60vh);
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	/* Section heading for grouped lists (model override: Claude + one per
	   enabled provider). Mirrors ModelPicker's `.mpk-grouphead`. */
	.override-dropdown-grouphead {
		padding: 8px 10px 4px;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	/* Auth-kind qualifier on a group heading ("· API" / "· PROLITE"). Lighter
	   and dimmer than the brand it follows, so the brand still reads first. */
	.override-dropdown-groupsub {
		font-weight: 500;
		opacity: 0.7;
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
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.override-dropdown-item .check-icon {
		flex-shrink: 0;
		color: var(--primary);
		margin-left: auto;
	}

	/* Mobile: slightly more compact, icon-only (labels stay hidden). */
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

	/* Desktop: labeled controls expand into full pills that match the composer's
	   top context pills (`.context-selector-btn` metrics: 28px tall, 0 10px pad,
	   same surface/border/radius/typography). Icon-only controls (no buttonLabel)
	   are untouched and stay compact. */
	@media (min-width: 640px) {
		.override-btn.has-label {
			width: auto;
			height: 28px;
			gap: 5px;
			padding: 0 10px;
			background: color-mix(in oklch, var(--foreground) 6%, var(--background));
			border: 1px solid var(--border);
			border-radius: var(--radius, 0.75rem);
			color: var(--muted-foreground);
			font-size: 0.75rem;
			font-weight: 500;
			white-space: nowrap;
		}

		.override-btn.has-label .override-label,
		.override-btn.has-label .override-chevron {
			display: block;
		}

		.override-btn.has-label:hover {
			background: color-mix(in oklch, var(--foreground) 6%, var(--background));
			border-color: var(--primary);
			color: var(--foreground);
		}

		/* Override set: primary-tinted chip instead of a corner dot. */
		.override-btn.has-label.active {
			background: color-mix(in oklch, var(--primary) 10%, var(--background));
			border-color: var(--primary);
			color: var(--foreground);
		}

		.override-btn.has-label.active:hover {
			background: color-mix(in oklch, var(--primary) 14%, var(--background));
		}

		/* Reroute: amber chip. The component never sets both `.active` and
		   `.warn` at once, so this rule doesn't depend on declaration order
		   relative to `.active`. */
		.override-btn.has-label.warn,
		.override-btn.has-label.warn:hover {
			background: color-mix(in oklch, var(--warning) 10%, var(--background));
			border-color: var(--warning);
			color: var(--warning);
		}

		/* The corner dot is redundant once the pill carries a label + fill. */
		.override-btn.has-label .override-indicator {
			display: none;
		}
	}
</style>
