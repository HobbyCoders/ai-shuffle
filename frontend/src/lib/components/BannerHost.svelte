<script lang="ts">
	/**
	 * Banner host — renders the global `liveBanners` slice (notifications emitted
	 * with `surface: 'banner'` via `notify.banner(...)`) as page-level, sticky
	 * messages. Mounted once at the app shell level above the page content.
	 *
	 * Page banners are sticky (no auto-dismiss) — they stay until the user closes
	 * them or `notify.dismissLive(id)` removes them. Styling/markup is the shared
	 * <Banner variant="page"> so they match the rest of the notification system.
	 */
	import { liveBanners, notify, type AppNotification } from '$lib/stores/notifications';
	import Banner from '$lib/components/Banner.svelte';

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

{#if banners.length > 0}
	<div class="banner-host" role="region" aria-label="Alerts">
		{#each banners as b (b.id)}
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
		{/each}
	</div>
{/if}

<style>
	/* In the desktop app the top 52px of the page is the window's title bar:
	   the sidebar head and top bar are drag regions, and Windows treats a
	   press there as a title-bar press however high the banner is stacked,
	   so its Undo and X never received the click. --titlebar-height exists
	   only on html.desktop, so the browser and phone shells keep the 12px
	   offset. */
	.banner-host {
		--banner-host-top: calc(var(--titlebar-height, 0px) + max(12px, env(safe-area-inset-top)));
		position: fixed;
		top: var(--banner-host-top);
		max-height: calc(100dvh - var(--banner-host-top) - 12px);
		overflow-y: auto;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		flex-direction: column;
		gap: 8px;
		width: min(640px, calc(100vw - 32px));
		z-index: 9998;
		pointer-events: none;
	}

	.banner-host :global(.banner) {
		pointer-events: auto;
		-webkit-app-region: no-drag;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
	}
</style>
