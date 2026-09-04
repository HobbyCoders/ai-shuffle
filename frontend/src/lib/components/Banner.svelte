<script lang="ts">
	/**
	 * Banner — the shared inline / page-level message surface for the unified
	 * notification system. Replaces the ~26 bespoke `.error-banner` /
	 * `.*-toast` blocks that each component used to hand-roll.
	 *
	 * Components keep their own local error state and simply render through this:
	 *   {#if error}
	 *     <Banner severity="error" message={error} dismissible onDismiss={() => (error = null)} />
	 *   {/if}
	 *
	 * For the bell, use `notify.*` from
	 * `$lib/stores/notifications` instead — this component is for in-flow messages.
	 */
	import { AlertTriangle, AlertOctagon, Info, CheckCircle2, X } from 'lucide-svelte';
	import type { Snippet } from 'svelte';
	import type { NotificationSeverity } from '$lib/stores/notifications';

	let {
		severity = 'error',
		variant = 'inline',
		message = '',
		title = '',
		dismissible = false,
		onDismiss,
		action,
		icon = true,
		children,
	}: {
		severity?: NotificationSeverity;
		variant?: 'inline' | 'page';
		message?: string;
		title?: string;
		dismissible?: boolean;
		onDismiss?: () => void;
		action?: { label: string; onClick: () => void };
		icon?: boolean;
		/** Optional rich content; overrides `message` when provided. */
		children?: Snippet;
	} = $props();

	const Icon = $derived(
		severity === 'error'
			? AlertOctagon
			: severity === 'warning'
				? AlertTriangle
				: severity === 'success'
					? CheckCircle2
					: Info
	);
</script>

<div
	class="banner sev-{severity} variant-{variant}"
	role={severity === 'error' ? 'alert' : 'status'}
>
	{#if icon}
		<span class="banner-icon" aria-hidden="true"><Icon size={variant === 'page' ? 18 : 15} /></span>
	{/if}
	<div class="banner-body">
		{#if title}<span class="banner-title">{title}</span>{/if}
		{#if children}
			{@render children()}
		{:else}
			<span class="banner-message">{message}</span>
		{/if}
	</div>
	{#if action}
		<button type="button" class="banner-action" onclick={action.onClick}>{action.label}</button>
	{/if}
	{#if dismissible}
		<button type="button" class="banner-close" onclick={() => onDismiss?.()} aria-label="Dismiss" title="Dismiss">
			<X size={14} />
		</button>
	{/if}
</div>

<style>
	.banner {
		display: flex;
		align-items: flex-start;
		gap: 9px;
		border: 1px solid var(--border);
		background: var(--card);
		color: var(--card-foreground, var(--foreground));
		box-shadow: 0 2px 8px rgb(0 0 0 / 0.06);
		box-sizing: border-box;
		flex-wrap: wrap;
		border-radius: calc(var(--radius, 0.75rem) * 0.66);
		font-size: 0.8125rem;
		line-height: 1.4;
	}

	.variant-inline {
		padding: 9px 12px;
		align-items: center;
	}

	.variant-page {
		padding: 12px 16px;
		width: 100%;
		font-size: 0.875rem;
	}

	/* ── Severity palettes (shared --status-* design tokens) ── */
	.sev-error {
		--banner-accent: var(--destructive);
	}
	.sev-warning {
		--banner-accent: var(--warning, #d97706);
	}
	.sev-success {
		--banner-accent: var(--success, #059669);
	}
	.sev-info {
		--banner-accent: var(--foreground);
	}

	.banner-icon {
		color: var(--banner-accent);
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		margin-top: 1px;
	}

	.banner-body {
		flex: 1 1 160px;
		min-width: 0;
		word-break: break-word;
	}

	.banner-title {
		display: block;
		font-weight: 600;
		margin-bottom: 2px;
	}

	.banner-message {
		display: block;
	}

	.banner-action {
		flex-shrink: 0;
		align-self: center;
		min-height: 28px;
		padding: 4px 12px;
		font-size: 0.75rem;
		font-weight: 600;
		background: var(--secondary);
		color: var(--secondary-foreground, var(--foreground));
		border: 1px solid var(--border);
		max-width: 100%;
		white-space: normal;
		border-radius: 6px;
		cursor: pointer;
		transition: opacity 0.15s ease;
	}
	.banner-action:hover {
		opacity: 0.88;
	}

	.banner-close {
		flex-shrink: 0;
		align-self: center;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 28px;
		min-height: 28px;
		padding: 2px;
		background: transparent;
		border: 0;
		border-radius: 6px;
		color: inherit;
		cursor: pointer;
		opacity: 0.75;
		transition: opacity 0.15s ease;
	}
	.banner-close:hover {
		opacity: 1;
	}

	.banner-action:focus-visible,
	.banner-close:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@media (max-width: 639px) {
		.banner-close {
			min-width: 40px;
			min-height: 40px;
		}
		.banner-action {
			min-height: 40px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.banner-action, .banner-close { transition: none; }
	}
</style>
