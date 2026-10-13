<script lang="ts">
	/**
	 * StudioEditorShell — shared Studio-v2 chrome for the inner editors
	 * (Profile / Subagent / MCP server / Hook / Automation).
	 *
	 * Provides the approved editor signature: an `.ed-bar` (back · title · actions)
	 * over an `.ed-layout` grid of a sticky side rail + a content panel, centered
	 * at 1240px. Pure app theme tokens — no Neo-Glass overlay on the chrome.
	 *
	 * The form bodies stay owned by their parents and render into the default
	 * slot. Editor content may freely use the grouped `.set-group` / `.set-row` /
	 * `.fld` primitives, which the shell exposes as themed `:global` styles.
	 */
	import type { Snippet } from 'svelte';
	import { ChevronLeft } from 'lucide-svelte';

	interface Section {
		id: string;
		label: string;
		/** lucide-svelte icon component */
		icon?: any;
		/** optional trailing count/badge (e.g. number of enabled items) */
		count?: number | string;
		/** render as a non-interactive group label in the rail (e.g. "Admin") */
		heading?: boolean;
	}

	interface Props {
		title: string;
		subtitle?: string;
		backLabel?: string;
		onBack?: () => void;
		/** rail sections; omit (or pass []) for a single-page, rail-less editor */
		sections?: Section[];
		active?: string;
		onSelect?: (id: string) => void;
		railLabel?: string;
		/** badge rendered immediately after the title (e.g. an "Active" pill) */
		titleBadge?: Snippet;
		/** right-aligned actions in the bar (Save, Duplicate, …) */
		actions?: Snippet;
		/** editor body — typically one panel per section */
		children: Snippet;
	}

	let {
		title,
		subtitle,
		backLabel,
		onBack,
		sections = [],
		active,
		onSelect,
		railLabel = 'Editor sections',
		titleBadge,
		actions,
		children
	}: Props = $props();

	const hasRail = $derived(sections.length > 0);
</script>

<div class="ed">
	<div class="ed-scroll">
		<div class="ed-wrap">
			<div class="ed-bar">
				{#if onBack}
					<button type="button" class="ed-back" onclick={onBack}>
						<ChevronLeft size={16} />
						<span>{backLabel ?? 'Back'}</span>
					</button>
				{/if}
				<div class="ed-ti">
					<h2>
						<span class="ed-ti-text">{title}</span>
						{#if titleBadge}{@render titleBadge()}{/if}
					</h2>
					{#if subtitle}<div class="ed-sub">{subtitle}</div>{/if}
				</div>
				{#if actions}
					<div class="ed-acts">{@render actions()}</div>
				{/if}
			</div>

			<div class="ed-layout" class:solo={!hasRail}>
				{#if hasRail}
					<nav class="ed-rail" aria-label={railLabel}>
						{#each sections as s (s.id)}
							{#if s.heading}
								<div class="ed-raillabel">{s.label}</div>
							{:else}
								{@const Icon = s.icon}
								<button
									type="button"
									class="ed-navitem"
									class:on={active === s.id}
									onclick={() => onSelect?.(s.id)}
								>
									{#if Icon}<Icon size={16} />{/if}
									<span class="ed-navitem-label">{s.label}</span>
									{#if s.count != null}<span class="ed-navitem-c">{s.count}</span>{/if}
								</button>
							{/if}
						{/each}
					</nav>
				{/if}

				<div class="ed-content" class:solo={!hasRail}>
					{@render children()}
				</div>
			</div>
		</div>
	</div>
</div>

<style>
	.ed {
		height: 100%;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.ed-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		overflow-x: hidden;
	}
	.ed-wrap {
		max-width: 1240px;
		margin: 0 auto;
		padding: 24px 28px 64px;
	}

	/* ── Top bar ── */
	.ed-bar {
		display: flex;
		align-items: center;
		gap: 14px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 14px 18px;
		margin-bottom: 18px;
	}
	.ed-back {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
		padding: 6px 10px 6px 7px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--background);
		color: var(--muted-foreground);
		font-size: 12.5px;
		font-weight: 500;
		cursor: pointer;
		transition: color 0.12s, background 0.12s, border-color 0.12s;
	}
	.ed-back:hover {
		color: var(--foreground);
		background: var(--muted);
		border-color: var(--muted-foreground);
	}
	.ed-ti {
		min-width: 0;
		flex: 1;
	}
	.ed-ti h2 {
		font-family: var(--font-display, Georgia, serif);
		font-weight: 800;
		font-size: 19px;
		margin: 0;
		letter-spacing: -0.01em;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 9px;
		min-width: 0;
	}
	.ed-ti-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ed-sub {
		font-size: 12px;
		color: var(--muted-foreground);
		margin-top: 3px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ed-acts {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	/* ── Layout: rail + content ── */
	.ed-layout {
		display: grid;
		grid-template-columns: 212px 1fr;
		gap: 26px;
		align-items: start;
	}
	/* Rail-less (single-page) editors: one centered column. */
	.ed-layout.solo {
		grid-template-columns: 1fr;
	}
	.ed-content.solo {
		max-width: 960px;
		margin: 0 auto;
		width: 100%;
	}
	.ed-rail {
		position: sticky;
		top: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.ed-navitem {
		display: flex;
		align-items: center;
		gap: 11px;
		padding: 9px 12px;
		border-radius: 9px;
		border: none;
		background: none;
		color: var(--muted-foreground);
		font-size: 13px;
		font-weight: 500;
		text-align: left;
		width: 100%;
		cursor: pointer;
		transition: background 0.12s, color 0.12s;
	}
	.ed-navitem :global(svg) {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}
	.ed-navitem:hover {
		background: var(--muted);
		color: var(--foreground);
	}
	.ed-navitem.on {
		background: var(--foreground);
		color: var(--background);
	}
	.ed-navitem-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ed-navitem-c {
		margin-left: auto;
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		opacity: 0.65;
	}
	/* Non-interactive group divider in the rail (e.g. "Admin") */
	.ed-raillabel {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--muted-foreground);
		padding: 14px 12px 5px;
		margin-top: 4px;
		border-top: 1px solid var(--border);
	}
	.ed-content {
		min-width: 0;
	}

	/* ── Responsive: rail folds above content ── */
	@media (max-width: 720px) {
		.ed-wrap {
			padding: 16px 16px 48px;
		}
		.ed-layout {
			grid-template-columns: 1fr;
			gap: 14px;
		}
		.ed-rail {
			position: static;
			flex-direction: row;
			flex-wrap: wrap;
			gap: 6px;
		}
		.ed-navitem {
			width: auto;
		}
		.ed-navitem-c {
			display: none;
		}
		.ed-raillabel {
			width: 100%;
			padding: 6px 2px 2px;
			margin-top: 2px;
			border-top: none;
		}
		.ed-bar {
			flex-wrap: wrap;
		}
		.ed-acts {
			width: 100%;
		}
	}

	/* ══════════════════════════════════════════════════════════════════
	   Shared grouped-field primitives for editor bodies.
	   Scoped under .ed-content so they only theme content inside this shell.
	   ══════════════════════════════════════════════════════════════════ */
	.ed-content :global(.set-group) {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
		margin-bottom: 18px;
	}
	.ed-content :global(.set-group:has(.mpk-panel)) {
		overflow: visible;
	}
	.ed-content :global(.set-group:last-child) {
		margin-bottom: 0;
	}
	.ed-content :global(.set-grouphead) {
		padding: 15px 18px 13px;
		border-bottom: 1px solid var(--border);
	}
	.ed-content :global(.set-grouphead h3) {
		font-family: var(--font-display, Georgia, serif);
		font-weight: 700;
		font-size: 16px;
		margin: 0;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 9px;
	}
	.ed-content :global(.set-grouphead h3 svg) {
		width: 17px;
		height: 17px;
		color: var(--muted-foreground);
	}
	.ed-content :global(.set-grouphead h3.danger),
	.ed-content :global(.set-row .lab h4.danger),
	.ed-content :global(.danger) {
		color: var(--destructive);
	}
	.ed-content :global(.set-grouphead p) {
		margin: 6px 0 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.ed-content :global(.set-row) {
		display: flex;
		align-items: center;
		gap: 18px;
		padding: 14px 18px;
		border-bottom: 1px solid var(--border);
	}
	.ed-content :global(.set-row:last-child) {
		border-bottom: none;
	}
	.ed-content :global(.set-row .lab) {
		min-width: 0;
		flex: 1;
	}
	.ed-content :global(.set-row .lab h4) {
		font-size: 13.5px;
		font-weight: 600;
		margin: 0 0 3px;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 7px;
		flex-wrap: wrap;
	}
	.ed-content :global(.set-row .lab h4 svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}
	.ed-content :global(.set-row .lab p) {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.45;
	}
	.ed-content :global(.set-row .ctl) {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 9px;
	}
	.ed-content :global(.set-row.stack) {
		display: block;
	}
	.ed-content :global(.set-row.stack .lab) {
		margin-bottom: 10px;
	}
	.ed-content :global(.set-row.stack .ctl) {
		display: block;
	}
	/* Inline inputs/selects span the full width when the row is stacked —
	   used for long free-text fields (paths, URLs) that need room. */
	.ed-content :global(.set-row.stack .ctl .inp),
	.ed-content :global(.set-row.stack .ctl .sel) {
		width: 100%;
	}
	.ed-content :global(.fld) {
		width: 100%;
		font: inherit;
		font-size: 13.5px;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 10px 12px;
	}
	.ed-content :global(.fld:focus) {
		outline: none;
		border-color: var(--ring, var(--primary));
	}
	.ed-content :global(textarea.fld) {
		resize: vertical;
		min-height: 96px;
		line-height: 1.6;
	}
	.ed-content :global(textarea.fld.code),
	.ed-content :global(.fld.mono) {
		font-family: var(--font-mono, monospace);
		font-size: 12.5px;
	}

	/* ── Shared bar/action buttons ── */
	/* Compact inline input — sits in a row's .ctl (label on the left). */
	.ed-content :global(.inp) {
		height: 36px;
		padding: 0 12px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--background);
		font: inherit;
		font-size: 13px;
		color: var(--foreground);
		min-width: 220px;
	}
	.ed-content :global(.inp.mono) {
		font-family: var(--font-mono, monospace);
		font-size: 12px;
	}
	.ed-content :global(.inp:focus),
	.ed-content :global(.sel:focus) {
		outline: none;
		border-color: var(--ring, var(--primary));
	}
	.ed-content :global(.inp::placeholder),
	.ed-content :global(.fld::placeholder) {
		color: var(--muted-foreground);
		opacity: 0.65;
	}

	/* Select — same metrics as .inp, with a themed chevron. */
	.ed-content :global(.sel) {
		height: 36px;
		padding: 0 32px 0 12px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--background);
		font: inherit;
		font-size: 13px;
		font-weight: 500;
		color: var(--foreground);
		appearance: none;
		cursor: pointer;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%239090a8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 10px center;
	}
	.ed-content :global(.sel.fld) {
		width: 100%;
		height: 38px;
	}

	/* Toggle switch — button.switch > <i>. On = primary (white) track. */
	.ed-content :global(.switch) {
		width: 34px;
		height: 20px;
		flex-shrink: 0;
		border-radius: 11px;
		background: var(--muted);
		border: 1px solid var(--border);
		position: relative;
		padding: 0;
		cursor: pointer;
		transition: background 0.18s, border-color 0.18s;
	}
	.ed-content :global(.switch i) {
		position: absolute;
		top: 1px;
		left: 1px;
		width: 15px;
		height: 15px;
		border-radius: 50%;
		background: var(--foreground);
		transition: left 0.18s, background 0.18s;
	}
	.ed-content :global(.switch.on) {
		background: var(--primary);
		border-color: var(--primary);
	}
	.ed-content :global(.switch.on i) {
		left: 16px;
		background: var(--primary-foreground);
	}
	.ed-content :global(.switch:disabled) {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* Segmented control. */
	.ed-content :global(.seg) {
		display: inline-flex;
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
		background: var(--background);
	}
	.ed-content :global(.seg button) {
		border: none;
		border-right: 1px solid var(--border);
		background: none;
		padding: 8px 13px;
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 0.12s, color 0.12s;
	}
	.ed-content :global(.seg button:last-child) {
		border-right: none;
	}
	.ed-content :global(.seg button:hover) {
		color: var(--foreground);
	}
	.ed-content :global(.seg button.on) {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	/* Small mono pill (e.g. "Next run" / counts). */
	.ed-content :global(.pill-s) {
		font-family: var(--font-mono, monospace);
		font-size: 10.5px;
		font-weight: 500;
		padding: 3px 8px;
		border-radius: 6px;
		background: var(--muted);
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.ed-content :global(.pill-s.acc) {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	/* Inline hint after a label / standalone gray helper. */
	.ed-content :global(.hint) {
		font-size: 11.5px;
		font-weight: 500;
		color: var(--muted-foreground);
	}

	/* Dashed info callout. */
	.ed-content :global(.ed-note) {
		display: flex;
		gap: 11px;
		padding: 13px 15px;
		border: 1px dashed var(--border);
		border-radius: 11px;
		font-size: 12.5px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.ed-content :global(.ed-note svg) {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		margin-top: 1px;
		color: var(--muted-foreground);
	}
	.ed-content :global(.ed-note a) {
		color: var(--foreground);
		text-decoration: underline;
	}

	.ed :global(.ed-btn) {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 7px 13px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--foreground);
		font-size: 12.5px;
		font-weight: 500;
		cursor: pointer;
		transition: background 0.12s, border-color 0.12s, opacity 0.12s;
	}
	.ed :global(.ed-btn:hover) {
		background: var(--muted);
	}
	.ed :global(.ed-btn.sm) {
		padding: 5px 10px;
		font-size: 12px;
	}
	.ed :global(.ed-btn.primary) {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}
	.ed :global(.ed-btn.primary:hover) {
		opacity: 0.9;
		background: var(--primary);
	}
	.ed :global(.ed-btn.danger) {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 35%, transparent);
		background: color-mix(in oklch, var(--destructive) 8%, transparent);
	}
	.ed :global(.ed-btn.danger:hover) {
		background: color-mix(in oklch, var(--destructive) 16%, transparent);
	}
	.ed :global(.ed-btn:disabled) {
		opacity: 0.45;
		cursor: not-allowed;
	}

	/* ── Scrollbar parity with other Studio-v2 surfaces ── */
	.ed-scroll::-webkit-scrollbar {
		width: 8px;
	}
	.ed-scroll::-webkit-scrollbar-track {
		background: transparent;
	}
	.ed-scroll::-webkit-scrollbar-thumb {
		background: var(--border);
		border-radius: 4px;
	}

	/* ══════════════════════════════════════════════════════════════════
	   Mobile — stack each row's label over its control so inputs get the
	   full width instead of being crushed against a 220px control column.
	   Declared last so it overrides the primitives above.
	   ══════════════════════════════════════════════════════════════════ */
	@media (max-width: 560px) {
		.ed-content :global(.set-row) {
			flex-direction: column;
			align-items: stretch;
			gap: 10px;
		}
		.ed-content :global(.set-row .ctl) {
			width: 100%;
			justify-content: flex-start;
		}
		.ed-content :global(.set-row .ctl .inp),
		.ed-content :global(.set-row .ctl .sel) {
			width: 100%;
			min-width: 0;
		}
	}
</style>
