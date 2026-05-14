<script lang="ts">
	/**
	 * Toast viewport — renders the global `toast` store as a stack of dismissible
	 * notifications. Mounted once at the app shell level. Slice C (SDK 0.1.81).
	 */
	import { toast, type Toast } from '$lib/stores/toast';
	import { AlertTriangle, AlertOctagon, Info, CheckCircle2, X } from 'lucide-svelte';

	const toastState = $derived($toast);

	function iconFor(t: Toast) {
		switch (t.tone) {
			case 'error':
				return AlertOctagon;
			case 'warning':
				return AlertTriangle;
			case 'success':
				return CheckCircle2;
			default:
				return Info;
		}
	}
</script>

{#if toastState.toasts.length > 0}
	<div class="toast-viewport" role="region" aria-label="Notifications" aria-live="polite">
		{#each toastState.toasts as t (t.id)}
			{@const Icon = iconFor(t)}
			<div class="toast tone-{t.tone}" role={t.tone === 'error' ? 'alert' : 'status'}>
				<div class="toast-icon" aria-hidden="true">
					<Icon size={16} />
				</div>
				<div class="toast-body">
					<span class="toast-message">{t.message}</span>
				</div>
				<button
					type="button"
					class="toast-close"
					onclick={() => toast.dismiss(t.id)}
					aria-label="Dismiss notification"
					title="Dismiss"
				>
					<X size={14} />
				</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	.toast-viewport {
		position: fixed;
		bottom: 16px;
		right: 16px;
		left: auto;
		display: flex;
		flex-direction: column-reverse;
		gap: 8px;
		z-index: 9999;
		max-width: min(420px, calc(100vw - 32px));
		pointer-events: none;
	}

	.toast {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 10px 12px;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: var(--card);
		color: var(--foreground);
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
		pointer-events: auto;
		animation: toast-enter 0.22s cubic-bezier(0.16, 1, 0.3, 1);
		min-width: 280px;
	}

	@keyframes toast-enter {
		from {
			opacity: 0;
			transform: translateY(6px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.tone-error {
		border-color: color-mix(in oklch, var(--destructive) 35%, var(--border));
		background: color-mix(in oklch, var(--destructive) 8%, var(--card));
	}

	.tone-warning {
		border-color: color-mix(in oklch, var(--warning, #d97706) 35%, var(--border));
		background: color-mix(in oklch, var(--warning, #d97706) 8%, var(--card));
	}

	.tone-info {
		border-color: var(--border);
	}

	.tone-success {
		border-color: color-mix(in oklch, var(--success, #059669) 35%, var(--border));
		background: color-mix(in oklch, var(--success, #059669) 8%, var(--card));
	}

	.toast-icon {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		margin-top: 1px;
	}

	.tone-error .toast-icon {
		color: var(--destructive);
	}

	.tone-warning .toast-icon {
		color: var(--warning, #d97706);
	}

	.tone-success .toast-icon {
		color: var(--success, #059669);
	}

	.tone-info .toast-icon {
		color: var(--muted-foreground);
	}

	.toast-body {
		flex: 1;
		min-width: 0;
		font-size: 0.8125rem;
		line-height: 1.4;
		word-break: break-word;
	}

	.toast-message {
		display: block;
	}

	.toast-close {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 32px;
		min-height: 32px;
		padding: 4px;
		background: transparent;
		border: 0;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.toast-close:hover {
		color: var(--foreground);
		background: color-mix(in oklch, var(--foreground) 8%, transparent);
	}

	.toast-close:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@media (max-width: 639px) {
		.toast-viewport {
			left: 16px;
			right: 16px;
			max-width: none;
		}

		.toast {
			min-width: 0;
			width: 100%;
		}

		.toast-close {
			min-width: 44px;
			min-height: 44px;
		}
	}
</style>
