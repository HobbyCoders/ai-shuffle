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
	import { AlertTriangle, AlertOctagon, Info, CheckCircle2, X, LoaderCircle } from 'lucide-svelte';
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
		secondaryAction,
		icon = true,
		children,
	}: {
		severity?: NotificationSeverity;
		variant?: 'inline' | 'page';
		message?: string;
		title?: string;
		dismissible?: boolean;
		onDismiss?: () => void;
		/** `busy`: the action is running. It shows a spinner, reports aria-busy
		 *  and ignores taps, but stays focusable so focus is not dropped. */
		action?: { label: string; onClick: () => void; disabled?: boolean; busy?: boolean };
		/** A second, quieter action after `action`. */
		secondaryAction?: { label: string; onClick: () => void };
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
		<button
			type="button"
			class="banner-action"
			class:is-busy={action.busy}
			onclick={() => { if (!action.busy) action.onClick(); }}
			disabled={action.disabled}
			aria-disabled={action.busy || undefined}
			aria-busy={action.busy || undefined}
		>
			{#if action.busy}<LoaderCircle size={13} aria-hidden="true" class="banner-action-spin" />{/if}
			<span>{action.label}</span>
		</button>
	{/if}
	{#if secondaryAction}
		<button type="button" class="banner-action secondary" onclick={secondaryAction.onClick}>{secondaryAction.label}</button>
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
		box-sizing: border-box;
		flex-wrap: wrap;
		border-radius: var(--radius);
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
		--banner-accent: var(--warning);
	}
	.sev-success {
		--banner-accent: var(--success);
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
		transition: opacity 0.15s ease, transform 0.1s ease-out, background-color 0.15s ease, border-color 0.15s ease;
	}
	.banner-action {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
	}
	.banner-action:hover:not(:disabled):not(.is-busy) {
		opacity: 0.88;
	}
	.banner-action:active:not(:disabled):not(.is-busy) {
		transform: scale(0.97);
	}
	/* Running, not unavailable: keep full contrast, tint it with the
	   banner's accent and show progress instead of a dimmed control. */
	.banner-action.is-busy {
		cursor: progress;
		border-color: color-mix(in oklch, var(--banner-accent) 45%, var(--border));
		background: color-mix(in oklch, var(--banner-accent) 12%, var(--secondary));
	}
	.banner-action :global(.banner-action-spin) {
		flex-shrink: 0;
		color: var(--banner-accent);
		animation: banner-action-spin 0.9s linear infinite;
	}
	@keyframes banner-action-spin { to { transform: rotate(360deg); } }
	.banner-action:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.banner-action.secondary {
		background: transparent;
	}

	.banner-close {
		flex-shrink: 0;
		align-self: center;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 32px;
		min-height: 32px;
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
		.banner-action {
			min-height: 40px;
		}
		/* One action plus the X: let the text narrow so the X stays on the
		   action's row instead of wrapping onto a row of its own. */
		.banner:not(:has(.banner-action.secondary)) .banner-body {
			flex-basis: 120px;
		}
	}
	/* Finger-sized X on narrow and touch screens; the phone shell sets the
	   same in mobile/src/mobile.css. */
	@media (max-width: 639px), (pointer: coarse) {
		.banner-close {
			min-width: 44px;
			min-height: 44px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.banner-action, .banner-close { transition: none; }
		.banner-action:active { transform: none; }
		.banner-action :global(.banner-action-spin) { animation-duration: 2.4s; }
	}
</style>
