<script lang="ts">
	/**
	 * Banner host — renders the global `liveBanners` slice (notifications emitted
	 * with `surface: 'banner'` via `notify.banner(...)`) as page-level, sticky
	 * messages. Mounted once at the app shell level alongside ToastViewport.
	 *
	 * Page banners are sticky (no auto-dismiss) — they stay until the user closes
	 * them or `notify.dismissLive(id)` removes them. Styling/markup is the shared
	 * <Banner variant="page"> so they match the rest of the notification system.
	 */
	import { liveBanners, notify, type AppNotification } from '$lib/stores/notifications';
	import Banner from '$lib/components/Banner.svelte';

	const banners = $derived($liveBanners);

	function actionFor(b: AppNotification) {
		if (!b.action) return undefined;
		const a = b.action;
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
	<div class="banner-host" role="region" aria-label="Alerts" aria-live="polite">
		{#each banners as b (b.id)}
			<Banner
				severity={b.severity}
				variant="page"
				title={b.title ?? ''}
				message={b.message}
				action={actionFor(b)}
				dismissible
				onDismiss={() => notify.dismissLive(b.id)}
			/>
		{/each}
	</div>
{/if}

<style>
	.banner-host {
		position: fixed;
		top: 12px;
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
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
	}
</style>
