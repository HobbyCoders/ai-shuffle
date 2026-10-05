<script lang="ts">
	/**
	 * Banner host: RootLayout's fallback notification dock (#1823).
	 *
	 * Page banners (`notify.banner(...)`) and every NotificationSlot draw in
	 * the shared NotificationDock. MainPanel mounts the anchored dock under its
	 * header; this fallback shows only on pages without MainPanel (setup,
	 * pairing), fixed under the desktop title-bar strip.
	 */
	import NotificationDock from './NotificationDock.svelte';
</script>

<NotificationDock variant="fallback" />
