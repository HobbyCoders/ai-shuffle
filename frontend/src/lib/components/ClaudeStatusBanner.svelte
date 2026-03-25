<script lang="ts">
	import { claudeStatus } from '$lib/stores/claude-status';

	let visible = $derived(
		!$claudeStatus.dismissed &&
		$claudeStatus.status !== null &&
		!$claudeStatus.status.ok
	);

	let description = $derived($claudeStatus.status?.description ?? '');
	let incidents = $derived($claudeStatus.status?.incidents ?? []);
	let firstIncident = $derived(incidents.length > 0 ? incidents[0] : null);

	// Use the highest-severity incident's impact for banner color (incidents are pre-sorted by severity)
	let indicator = $derived(firstIncident?.impact ?? $claudeStatus.status?.indicator ?? 'minor');

	function dismiss() {
		claudeStatus.dismiss();
	}

	function truncate(text: string, maxLen: number): string {
		if (text.length <= maxLen) return text;
		return text.slice(0, maxLen).trimEnd() + '...';
	}
</script>

{#if visible}
	<div
		class="claude-status-banner"
		class:severity-minor={indicator === 'minor'}
		class:severity-major={indicator === 'major'}
		class:severity-critical={indicator === 'critical'}
		role="alert"
		aria-live="polite"
	>
		<div class="banner-inner">
			<!-- Warning icon -->
			<div class="banner-icon">
				<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
					<line x1="12" y1="9" x2="12" y2="13" />
					<line x1="12" y1="17" x2="12.01" y2="17" />
				</svg>
			</div>

			<!-- Content -->
			<div class="banner-content">
				<span class="banner-title">Claude is experiencing issues</span>
				{#if description}
					<span class="banner-description">{description}</span>
				{/if}
				{#if firstIncident}
					<span class="banner-incident">
						{truncate(firstIncident.name, 80)}
						{#if firstIncident.latest_update}
							<span class="banner-incident-update">
								&mdash; {truncate(firstIncident.latest_update, 120)}
							</span>
						{/if}
					</span>
				{/if}
			</div>

			<!-- Status link + dismiss -->
			<div class="banner-actions">
				<a
					href="https://status.claude.com"
					target="_blank"
					rel="noopener noreferrer"
					class="banner-link"
				>
					View Status
					<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
						<path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6" />
						<polyline points="15 3 21 3 21 9" />
						<line x1="10" y1="14" x2="21" y2="3" />
					</svg>
				</a>
				<button
					onclick={dismiss}
					class="banner-dismiss"
					aria-label="Dismiss status banner"
				>
					<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
						<line x1="18" y1="6" x2="6" y2="18" />
						<line x1="6" y1="6" x2="18" y2="18" />
					</svg>
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.claude-status-banner {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		z-index: 100;
		animation: slideDown 0.3s ease-out;
	}

	@keyframes slideDown {
		from {
			transform: translateY(-100%);
			opacity: 0;
		}
		to {
			transform: translateY(0);
			opacity: 1;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.claude-status-banner {
			animation: none;
		}
	}

	/* Severity color schemes */
	.severity-minor {
		background: hsl(45, 93%, 47%);
		color: hsl(45, 100%, 10%);
	}
	.severity-major {
		background: hsl(25, 95%, 53%);
		color: hsl(25, 100%, 10%);
	}
	.severity-critical {
		background: hsl(0, 84%, 60%);
		color: white;
	}

	.banner-inner {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 16px;
		max-width: 1400px;
		margin: 0 auto;
	}

	.banner-icon {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		opacity: 0.85;
	}

	.banner-content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 4px 12px;
	}

	.banner-title {
		font-size: 14px;
		font-weight: 600;
		white-space: nowrap;
	}

	.banner-description {
		font-size: 13px;
		opacity: 0.85;
		white-space: nowrap;
	}

	.banner-incident {
		font-size: 12px;
		opacity: 0.75;
		width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.banner-incident-update {
		opacity: 0.8;
	}

	.banner-actions {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.banner-link {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 12px;
		font-weight: 500;
		text-decoration: none;
		color: inherit;
		opacity: 0.85;
		white-space: nowrap;
		padding: 4px 8px;
		border-radius: 6px;
		transition: opacity 0.15s ease;
	}

	.banner-link:hover {
		opacity: 1;
		text-decoration: underline;
	}

	.banner-dismiss {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		min-width: 44px;
		min-height: 44px;
		padding: 0;
		border: none;
		background: transparent;
		color: inherit;
		opacity: 0.7;
		cursor: pointer;
		border-radius: 8px;
		transition: opacity 0.15s ease, background-color 0.15s ease;
	}

	.banner-dismiss:hover {
		opacity: 1;
		background: rgba(0, 0, 0, 0.1);
	}

	.severity-critical .banner-dismiss:hover {
		background: rgba(255, 255, 255, 0.15);
	}

	.banner-dismiss:focus-visible {
		outline: 2px solid currentColor;
		outline-offset: -2px;
		opacity: 1;
	}

	/* Mobile: stack content vertically */
	@media (max-width: 640px) {
		.banner-inner {
			flex-wrap: wrap;
			padding: 10px 12px;
		}

		.banner-content {
			flex-basis: calc(100% - 80px);
		}

		.banner-actions {
			margin-left: auto;
		}

		.banner-incident {
			white-space: normal;
		}
	}
</style>
