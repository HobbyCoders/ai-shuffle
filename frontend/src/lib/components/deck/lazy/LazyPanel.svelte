<script lang="ts">
	/**
	 * LazyPanel — renders a lazily-imported feature surface.
	 *
	 * Rules this encodes so no caller has to think about them:
	 *  • A handle that is already resolved renders in the SAME frame — a revisit
	 *    (or a panel warmed by hover/idle prefetch) never shows a skeleton.
	 *  • The skeleton only appears after `skeletonDelayMs`, so a fast chunk
	 *    fetch shows nothing at all rather than a 40 ms flash.
	 *  • A failed chunk fetch shows a real error with a Retry that re-attempts
	 *    the import (the handle deliberately does not cache rejections), instead
	 *    of leaving a blank panel.
	 */
	import type { LazyComponentHandle } from './lazy-component';

	interface Props {
		/** The lazy handle for this surface (see `./panels.ts`). */
		handle: LazyComponentHandle<any>;
		/** Props spread onto the loaded component. */
		props?: Record<string, unknown>;
		/** Human name used in the loading/error copy ("Settings", "Docs"…). */
		label?: string;
		/**
		 * Render nothing while loading or on failure. For invisible surfaces
		 * (modals, the collapsed browser panel) a skeleton would be wrong.
		 */
		bare?: boolean;
		/** Grace period before the skeleton appears. */
		skeletonDelayMs?: number;
	}

	let { handle, props = {}, label = 'panel', bare = false, skeletonDelayMs = 140 }: Props = $props();

	// Seed from the cache so an already-loaded chunk renders synchronously.
	// Reading the prop once at init is deliberate: the effect below re-syncs on
	// every handle change, so we only need the initial cache hit here.
	// svelte-ignore state_referenced_locally
	let Component = $state<any>(handle.resolved);
	let failed = $state(false);
	let showSkeleton = $state(false);
	let attempt = $state(0);

	$effect(() => {
		const target = handle;
		attempt; // retry trigger

		if (target.resolved) {
			Component = target.resolved;
			failed = false;
			showSkeleton = false;
			return;
		}

		Component = null;
		failed = false;
		let cancelled = false;

		const skeletonTimer = setTimeout(() => {
			if (!cancelled) showSkeleton = true;
		}, skeletonDelayMs);

		target
			.load()
			.then((component) => {
				if (cancelled) return;
				Component = component;
				failed = false;
			})
			.catch((err) => {
				if (cancelled) return;
				console.error(`[lazy-panel] failed to load ${target.id}`, err);
				failed = true;
			})
			.finally(() => {
				clearTimeout(skeletonTimer);
				if (!cancelled) showSkeleton = false;
			});

		return () => {
			cancelled = true;
			clearTimeout(skeletonTimer);
		};
	});

	function retry() {
		failed = false;
		attempt += 1;
	}
</script>

{#if Component}
	<Component {...props} />
{:else if failed && !bare}
	<div class="lazy-error" role="alert">
		<p class="lazy-error-title">Couldn’t load {label}.</p>
		<p class="lazy-error-hint">
			The connection dropped, or the app was updated while this tab was open.
		</p>
		<div class="lazy-error-actions">
			<button type="button" class="lazy-btn" onclick={retry}>Retry</button>
			<button type="button" class="lazy-btn ghost" onclick={() => location.reload()}>
				Reload app
			</button>
		</div>
	</div>
{:else if showSkeleton && !bare}
	<div class="lazy-skeleton" aria-busy="true" aria-label={`Loading ${label}`}>
		<div class="sk sk-title"></div>
		<div class="sk sk-sub"></div>
		<div class="sk-rows">
			<div class="sk sk-row"></div>
			<div class="sk sk-row"></div>
			<div class="sk sk-row"></div>
			<div class="sk sk-row"></div>
		</div>
	</div>
{/if}

<style>
	.lazy-skeleton {
		flex: 1;
		min-height: 0;
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		padding: 24px 28px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.sk {
		border-radius: 8px;
		background: color-mix(in oklch, var(--muted) 55%, transparent);
		animation: lazy-pulse 1.4s ease-in-out infinite;
	}
	.sk-title { height: 26px; width: 180px; }
	.sk-sub { height: 13px; width: 300px; opacity: 0.75; }
	.sk-rows { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; }
	.sk-row { height: 56px; width: 100%; opacity: 0.6; }

	@keyframes lazy-pulse {
		0%, 100% { opacity: 0.55; }
		50% { opacity: 0.85; }
	}
	@media (prefers-reduced-motion: reduce) {
		.sk { animation: none; }
	}

	.lazy-error {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 32px;
		text-align: center;
		color: var(--muted-foreground);
	}
	.lazy-error-title {
		margin: 0;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--foreground);
	}
	.lazy-error-hint { margin: 0; font-size: 0.8125rem; max-width: 34ch; }
	.lazy-error-actions { display: flex; gap: 8px; margin-top: 10px; }
	.lazy-btn {
		padding: 6px 14px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		border: 1px solid var(--border);
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
	}
	.lazy-btn.ghost { background: transparent; color: var(--foreground); }
	.lazy-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
</style>
