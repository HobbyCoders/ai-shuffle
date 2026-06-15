<script lang="ts">
	/**
	 * ClaudeStatusIndicator - Compact Claude service-status button for the header.
	 *
	 * Always-visible small icon button reflecting Claude's current service
	 * status (operational / minor / major). Click toggles a popover with
	 * details and a link to the public status page. Closes on outside-click
	 * and Escape, mirroring the dropdown pattern in GitHeaderActions.
	 *
	 * Replaces the old full-width ClaudeStatusBanner. The polling lifecycle
	 * still lives in the claudeStatus store (started/stopped from +layout).
	 */
	import { CircleCheck, TriangleAlert, OctagonX, ExternalLink } from 'lucide-svelte';
	import { claudeStatus } from '$lib/stores/claude-status';

	type Severity = 'operational' | 'minor' | 'major';

	const status = $derived($claudeStatus.status);
	const isOk = $derived(!status || status.ok);
	const description = $derived(status?.description ?? '');
	const incidents = $derived(status?.incidents ?? []);
	const firstIncident = $derived(incidents.length > 0 ? incidents[0] : null);

	// Compute severity from the highest-severity incident's impact when present
	// (incidents are pre-sorted by severity), else fall back to the top-level
	// indicator. 'critical' is treated the same as 'major' visually (red).
	const severity = $derived<Severity>(
		(() => {
			if (isOk) return 'operational';
			const impact = firstIncident?.impact ?? status?.indicator ?? 'minor';
			if (impact === 'major' || impact === 'critical') return 'major';
			if (impact === 'minor') return 'minor';
			return 'operational';
		})()
	);

	const title = $derived(isOk ? 'All systems operational' : 'Claude is experiencing issues');

	let showPopover = $state(false);
	let rootRef = $state<HTMLDivElement | null>(null);

	function togglePopover() {
		showPopover = !showPopover;
	}

	function handleClickOutside(e: MouseEvent) {
		if (!showPopover) return;
		if (rootRef && !rootRef.contains(e.target as Node)) {
			showPopover = false;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showPopover) {
			showPopover = false;
		}
	}

	function truncate(text: string, maxLen: number): string {
		if (text.length <= maxLen) return text;
		return text.slice(0, maxLen).trimEnd() + '...';
	}
</script>

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} />

<div class="status-indicator" bind:this={rootRef}>
	<button
		class="status-btn"
		class:operational={severity === 'operational'}
		class:minor={severity === 'minor'}
		class:major={severity === 'major'}
		class:open={showPopover}
		onclick={togglePopover}
		aria-haspopup="dialog"
		aria-expanded={showPopover}
		aria-label="Claude service status: {title}"
		title={title}
	>
		{#if severity === 'operational'}
			<CircleCheck size={15} strokeWidth={1.5} />
		{:else if severity === 'minor'}
			<TriangleAlert size={15} strokeWidth={1.5} />
		{:else}
			<OctagonX size={15} strokeWidth={1.5} />
		{/if}
	</button>

	{#if showPopover}
		<div class="status-popover" role="dialog" aria-label="Claude service status">
			<div class="popover-header">
				<span
					class="popover-dot"
					class:operational={severity === 'operational'}
					class:minor={severity === 'minor'}
					class:major={severity === 'major'}
					aria-hidden="true"
				></span>
				<span class="popover-title">{title}</span>
			</div>

			{#if description}
				<p class="popover-description">{description}</p>
			{/if}

			{#if firstIncident}
				<div class="popover-incident">
					<span class="incident-name">{truncate(firstIncident.name, 120)}</span>
					{#if firstIncident.latest_update}
						<span class="incident-update">{truncate(firstIncident.latest_update, 200)}</span>
					{/if}
				</div>
			{/if}

			<a
				href="https://status.claude.com"
				target="_blank"
				rel="noopener noreferrer"
				class="popover-link"
			>
				View Status
				<ExternalLink size={12} strokeWidth={1.5} />
			</a>
		</div>
	{/if}
</div>

<style>
	.status-indicator {
		position: relative;
		display: flex;
		align-items: center;
	}

	/* Always-visible icon button — real tap target on mobile (padded). */
	.status-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}

	.status-btn:hover,
	.status-btn.open {
		background: var(--hover-overlay);
	}

	.status-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* Operational: subtle / unobtrusive. */
	.status-btn.operational {
		color: var(--muted-foreground);
		opacity: 0.65;
	}

	.status-btn.operational:hover,
	.status-btn.operational.open {
		opacity: 1;
		color: var(--muted-foreground);
	}

	/* Minor: amber warning. */
	.status-btn.minor {
		color: var(--warning, #e8a735);
	}

	.status-btn.minor:hover,
	.status-btn.minor.open {
		background: color-mix(in oklch, var(--warning, #e8a735) 12%, transparent);
		color: var(--warning, #e8a735);
	}

	/* Major / critical: red. */
	.status-btn.major {
		color: var(--destructive);
	}

	.status-btn.major:hover,
	.status-btn.major.open {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
	}

	/* Popover anchored to the right edge of the button. */
	.status-popover {
		position: absolute;
		top: calc(100% + 6px);
		right: 0;
		z-index: 100;
		width: max-content;
		max-width: min(320px, calc(100vw - 24px));
		padding: 12px 14px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		animation: popover-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes popover-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.popover-header {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.popover-dot {
		flex-shrink: 0;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--muted-foreground);
	}

	.popover-dot.operational {
		background: var(--success, #22c55e);
	}

	.popover-dot.minor {
		background: var(--warning, #e8a735);
	}

	.popover-dot.major {
		background: var(--destructive);
	}

	.popover-title {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
		line-height: 1.3;
	}

	.popover-description {
		margin: 8px 0 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	.popover-incident {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 10px;
		padding-top: 10px;
		border-top: 1px solid var(--border);
	}

	.incident-name {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
		line-height: 1.4;
	}

	.incident-update {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	.popover-link {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		margin-top: 12px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--primary);
		text-decoration: none;
		transition: opacity 150ms ease;
	}

	.popover-link:hover {
		text-decoration: underline;
	}

	.popover-link:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 2px;
	}

	/* Mobile: ensure a >=36px tap target. */
	@media (max-width: 639px) {
		.status-btn {
			width: 36px;
			height: 36px;
		}

		.status-popover {
			max-width: calc(100vw - 24px);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.status-btn {
			transition: none;
		}
		.status-popover {
			animation: none;
		}
	}
</style>
