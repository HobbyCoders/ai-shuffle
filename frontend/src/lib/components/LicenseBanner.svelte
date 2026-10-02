<script lang="ts">
	/**
	 * App-wide license notice: payment grace, a lease about to run out, or an
	 * unlicensed worker (new agent runs paused, data untouched). Silent in beta
	 * builds and when everything is fine.
	 */
	import { onMount } from 'svelte';
	import { getLicense, LICENSE_CHANGED_EVENT, type LicenseStatus } from '$lib/api/license';
	import { openSettings } from '$lib/settings/navigation.svelte';
	import Banner from './Banner.svelte';

	const POLL_MS = 30 * 60 * 1000;
	let status = $state<LicenseStatus | null>(null);
	let dismissed = $state<string | null>(null);

	async function load() {
		try { status = await getLicense(); } catch { /* keep the last known status */ }
	}

	onMount(() => {
		void load();
		const timer = setInterval(load, POLL_MS);
		window.addEventListener(LICENSE_CHANGED_EVENT, load);
		return () => { clearInterval(timer); window.removeEventListener(LICENSE_CHANGED_EVENT, load); };
	});

	const banner = $derived(status?.mode === 'licensed' ? status.banner : null);
	// Warnings can be dismissed for this page load; a blocked worker cannot.
	const visible = $derived(!!banner && (banner.severity === 'error' || dismissed !== banner.message));
</script>

{#if banner && visible}
	<Banner
		title={banner.severity === 'error' ? 'License' : 'License notice'}
		message={banner.message}
		action={{ label: 'Open license', onClick: () => openSettings({ category: 'system', anchor: 'system.license' }) }}
		severity={banner.severity}
		dismissible={banner.severity !== 'error'}
		onDismiss={() => (dismissed = banner.message)}
	/>
{/if}
