<script lang="ts">
	/**
	 * NotificationDock: the one container every notification draws in (#1823).
	 *
	 * `anchored` (MainPanel): absolutely positioned in the main content area,
	 * so it starts just under the page header, and as wide as the chat column
	 * (`span`), so it stays centred on the chat when the sidebar or a side panel
	 * is open. Being under the header in both layout and document order, it
	 * never overlaps the desktop window's drag strip (the top bar).
	 *
	 * `fallback` (RootLayout): fixed under the title-bar strip, shown only while
	 * no anchored dock is mounted (setup and pairing pages).
	 *
	 * The container and the stack ignore the pointer; only each card takes
	 * clicks, so the clickable box is exactly the drawn box. No transform
	 * moves the container, and cards fade in without moving.
	 */
	import { onMount } from 'svelte';
	import { liveBanners, notify, type AppNotification } from '$lib/stores/notifications';
	import { notificationDock, DOCK_ORDER } from '$lib/stores/notification-dock.svelte';
	import Banner from '$lib/components/Banner.svelte';

	let { variant = 'anchored', span }: { variant?: 'anchored' | 'fallback'; span?: number } = $props();

	onMount(() => notificationDock.mount(variant));

	const active = $derived(variant === 'anchored' || notificationDock.anchored === 0);
	const entries = $derived(notificationDock.sorted);
	const before = $derived(entries.filter((e) => e.order < DOCK_ORDER.banners));
	const after = $derived(entries.filter((e) => e.order >= DOCK_ORDER.banners));
	const banners = $derived($liveBanners);

	function actionFor(b: AppNotification, which: 'action' | 'secondaryAction' = 'action') {
		const a = b[which];
		if (!a) return undefined;
		return {
			label: a.label,
			onClick: () => {
				a.onClick();
				notify.dismissLive(b.id);
			},
		};
	}
</script>

{#if active && (entries.length > 0 || banners.length > 0)}
	<div
		class="notification-dock {variant}"
		role="region"
		aria-label="Alerts"
		data-testid="notification-dock"
		style:--dock-span={span ? `${span}px` : undefined}
	>
		<div class="dock-stack">
			{#each before as e (e.id)}
				<div class="dock-entry">{@render e.render()}</div>
			{/each}
			{#each banners as b (b.id)}
				<div class="dock-entry">
					<Banner
						severity={b.severity}
						variant="page"
						title={b.title ?? ''}
						message={b.message}
						action={actionFor(b)}
						secondaryAction={actionFor(b, 'secondaryAction')}
						dismissible
						onDismiss={() => notify.dismissLive(b.id)}
					/>
				</div>
			{/each}
			{#each after as e (e.id)}
				<div class="dock-entry">{@render e.render()}</div>
			{/each}
		</div>
	</div>
{/if}

<style>
	.notification-dock {
		--dock-gap: 8px;
		display: flex;
		justify-content: center;
		box-sizing: border-box;
		padding: 0 12px 12px;
		overflow-y: auto;
		overscroll-behavior: contain;
		pointer-events: none;
		/* Electron hands any press inside a drag region to the OS title bar.
		   The dock sits below the drag strip; this keeps it out of any. */
		-webkit-app-region: no-drag;
		app-region: no-drag;
	}

	/* Under the page header, centred on the chat column. */
	.notification-dock.anchored {
		position: absolute;
		top: var(--dock-gap);
		left: 0;
		width: var(--dock-span, 100%);
		max-width: 100%;
		max-height: calc(100% - var(--dock-gap));
		/* Above the phone drawer (100), so an Undo after archiving from it shows. */
		z-index: 150;
	}

	/* Pages without the main panel: under the title-bar strip. */
	.notification-dock.fallback {
		--dock-top: calc(var(--titlebar-height, 0px) + max(12px, env(safe-area-inset-top)));
		position: fixed;
		top: var(--dock-top);
		left: 0;
		right: 0;
		max-height: calc(100dvh - var(--dock-top));
		z-index: 9998;
	}

	.dock-stack {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--dock-gap);
		width: min(640px, 100%);
		min-width: 0;
		pointer-events: none;
	}

	.dock-entry {
		display: flex;
		justify-content: center;
		width: 100%;
		min-width: 0;
		pointer-events: none;
	}

	/* The card itself is the only click target. */
	.dock-entry > :global(*) {
		pointer-events: auto;
		max-width: 100%;
		box-sizing: border-box;
		-webkit-app-region: no-drag;
		app-region: no-drag;
		animation: dock-in 160ms ease-out;
	}
	.dock-entry > :global(.banner) {
		box-shadow: var(--shadow-l, 0 8px 24px rgb(0 0 0 / 0.18));
	}

	@keyframes dock-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}
	@media (prefers-reduced-motion: reduce) {
		.dock-entry > :global(*) { animation: none; }
	}
</style>
