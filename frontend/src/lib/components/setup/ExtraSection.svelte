<script lang="ts">
	/**
	 * One optional part of setup's last step, as a summary row that expands.
	 *
	 * The row says what the part is and where it stands ("Connected as
	 * @octocat", "8 conversations found"); its content opens below it. The
	 * content stays mounted while collapsed, so a sign-in that is under way
	 * keeps polling and its state survives closing the row.
	 */
	import type { Snippet } from 'svelte';
	import { Check, ChevronDown, Loader2, AlertCircle } from 'lucide-svelte';
	import type { ExtraTone } from './extras-summary';

	interface Props {
		/** Stable id: the panel's DOM id and the test id suffix. */
		id: string;
		title: string;
		summary: string;
		tone?: ExtraTone;
		icon: typeof Check;
		/** The row's call to action while nothing is set up yet ("Sign in", "Connect"). */
		action?: string;
		open?: boolean;
		/** Keeps the content mounted but shows nothing (a part with nothing to offer yet). */
		hidden?: boolean;
		children: Snippet;
	}
	let { id, title, summary, tone = 'idle', icon: Icon, action: idleAction = 'Set up', open = $bindable(false), hidden = false, children }: Props = $props();

	const panelId = $derived(`setup-extra-${id}-panel`);
	const action = $derived(open ? 'Close' : tone === 'done' ? 'Manage' : tone === 'idle' ? idleAction : 'Open');
</script>

<section class="extra" class:open data-tone={tone} {hidden} data-testid={`setup-extra-${id}`} aria-label={title}>
	<h3 class="extra-heading">
		<button type="button" class="extra-row" aria-expanded={open} aria-controls={panelId} onclick={() => (open = !open)}>
			<span class="extra-icon" aria-hidden="true">
				<Icon size={18} strokeWidth={1.75} />
				{#if tone === 'done'}<span class="extra-badge"><Check size={10} strokeWidth={3} /></span>{/if}
			</span>
			<span class="extra-text">
				<span class="extra-title">{title}</span>
				<span class="extra-summary" data-testid={`setup-extra-${id}-summary`}>
					{#if tone === 'busy'}<Loader2 size={12} strokeWidth={2.25} class="animate-spin shrink-0" aria-hidden="true" />{:else if tone === 'error'}<AlertCircle size={12} strokeWidth={2.25} class="shrink-0" aria-hidden="true" />{/if}
					<span>{summary}</span>
				</span>
			</span>
			<span class="extra-action" aria-hidden="true">{action}</span>
			<ChevronDown size={16} strokeWidth={2} class="extra-chevron" aria-hidden="true" />
		</button>
	</h3>
	<div id={panelId} class="extra-panel" hidden={!open}>
		{@render children()}
	</div>
</section>

<style>
	.extra {
		border: 1px solid var(--border);
		border-radius: var(--radius, 10px);
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		transition: border-color 0.2s ease-out, background-color 0.2s ease-out;
	}
	.extra[hidden] { display: none; }
	.extra.open {
		border-color: color-mix(in oklch, var(--primary) 35%, var(--border));
		background: var(--card);
	}
	.extra-heading { margin: 0; font: inherit; }
	.extra-row {
		display: flex; align-items: center; gap: 12px;
		width: 100%; min-height: 60px; padding: 10px 14px;
		border: 0; border-radius: inherit; background: transparent;
		color: var(--foreground); font: inherit; text-align: left; cursor: pointer;
	}
	.extra.open .extra-row {
		border-bottom-left-radius: 0; border-bottom-right-radius: 0;
		background: color-mix(in oklch, var(--muted) 45%, transparent);
	}
	.extra-row:hover, .extra.open .extra-row:hover { background: var(--hover-overlay); }
	.extra-row:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.extra-icon {
		position: relative; flex: none;
		display: flex; align-items: center; justify-content: center;
		width: 36px; height: 36px; border-radius: 9px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}
	[data-tone='done'] .extra-icon { background: color-mix(in oklch, var(--success) 14%, transparent); color: var(--success); }
	[data-tone='error'] .extra-icon { background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); }
	.extra-badge {
		position: absolute; right: -4px; bottom: -4px;
		display: flex; align-items: center; justify-content: center;
		width: 16px; height: 16px; border-radius: 50%;
		background: var(--success); color: var(--success-foreground);
		box-shadow: 0 0 0 2px var(--card);
	}

	.extra-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
	.extra-title { font-size: 0.875rem; font-weight: 600; line-height: 1.35; }
	.extra-summary {
		display: flex; align-items: center; gap: 6px;
		font-size: 0.8125rem; line-height: 1.4; color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
	[data-tone='done'] .extra-summary { color: var(--success); }
	[data-tone='error'] .extra-summary { color: var(--destructive); }

	.extra-action {
		flex: none; padding: 3px 10px;
		border: 1px solid var(--border); border-radius: 999px;
		font-size: 0.75rem; font-weight: 500; color: var(--muted-foreground);
		transition: color 0.2s ease-out, border-color 0.2s ease-out;
	}
	.extra-row:hover .extra-action { color: var(--foreground); border-color: var(--border-strong, var(--border)); }
	.extra-row :global(.extra-chevron) { flex: none; color: var(--muted-foreground); transition: transform 0.2s ease-out; }
	.extra.open .extra-row :global(.extra-chevron) { transform: rotate(180deg); }

	.extra-panel {
		padding: 14px;
		border-top: 1px solid var(--border);
		animation: extra-reveal 0.22s ease-out;
	}
	@keyframes extra-reveal {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: none; }
	}

	/* A phone has no room for the action pill; the chevron says the same. */
	@media (max-width: 520px) {
		.extra-row { padding: 10px 12px; gap: 10px; }
		.extra-action { display: none; }
		.extra-panel { padding: 12px; }
	}
	@media (prefers-reduced-motion: reduce) {
		.extra, .extra-action, .extra-row :global(.extra-chevron) { transition: none; }
		.extra-panel { animation: none; }
	}
</style>
