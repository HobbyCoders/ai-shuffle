<script lang="ts">
	// The real error-notification entry point (a page banner built by
	// errorActions) plus the app-wide report dialog host. Every API call is
	// answered by verify-support-report.mjs in the browser.
	import { onMount } from 'svelte';
	import BannerHost from '../src/lib/components/BannerHost.svelte';
	import SupportReportHost from '../src/lib/components/support/SupportReportHost.svelte';
	import { notify } from '../src/lib/stores/notifications';
	import { errorActions } from '../src/lib/services/error-actions';
	import { supportCaps } from '../src/lib/stores/support-report.svelte';
	import { hydrateModelRegistry } from '../src/lib/constants/models';

	onMount(async () => {
		await hydrateModelRegistry();
		await supportCaps.load();
		notify.banner('API error: HTTP 500', {
			severity: 'error',
			title: 'Request failed',
			...errorActions({ source: 'ws', message: 'API error: HTTP 500', status_code: 500, session_id: 's-1' })
		});
		(window as unknown as { __ready: boolean }).__ready = true;
	});
</script>

<main class="page">
	<h1>AI Shuffle</h1>
	<p>Fixture page behind the dialog.</p>
</main>
<BannerHost />
<SupportReportHost />

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.page { padding: 96px 24px; max-width: 720px; margin: 0 auto; color: var(--muted-foreground); }
	h1 { color: var(--foreground); font-size: 20px; }
</style>
