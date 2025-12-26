<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { auth, isAuthenticated, setupRequired, claudeAuthenticated } from '$lib/stores/auth';
	import { theme } from '$lib/stores/theme';
	import { pwa, hasUpdate, isOnline } from '$lib/stores/pwa';
	import InstallPrompt from '$lib/components/InstallPrompt.svelte';
	import UpdatePrompt from '$lib/components/UpdatePrompt.svelte';
	import OfflineBanner from '$lib/components/OfflineBanner.svelte';

	let initialized = false;

	onMount(async () => {
		// Initialize theme on mount
		theme.init();

		// Initialize PWA features
		pwa.init();

		try {
			await auth.checkAuth();
			initialized = true;

			// Handle redirects
			const path = $page.url.pathname;

			if ($setupRequired && path !== '/setup') {
				goto('/setup');
			} else if (!$isAuthenticated && !$setupRequired && path !== '/login') {
				goto('/login');
			} else if ($isAuthenticated && (path === '/login' || path === '/setup')) {
				goto('/');
			}
		} catch (e) {
			console.error('Auth check failed:', e);
			initialized = true;
		}
	});
</script>

<svelte:head>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
	<meta name="theme-color" content="#0f0f0f" />
	<meta name="mobile-web-app-capable" content="yes" />
	<meta name="apple-mobile-web-app-capable" content="yes" />
	<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
	<!-- Prevent flash of wrong theme by applying theme class before paint -->
	{@html `<script>
		(function() {
			var stored = localStorage.getItem('ai-shuffle-theme');
			var theme = stored || 'system';
			var resolved = theme;
			if (theme === 'system') {
				resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
			}
			document.documentElement.classList.add(resolved);
			document.documentElement.classList.remove(resolved === 'dark' ? 'light' : 'dark');
			var meta = document.querySelector('meta[name="theme-color"]');
			if (meta) meta.setAttribute('content', resolved === 'dark' ? '#0f0f0f' : '#ffffff');
		})();
	</script>`}
</svelte:head>

{#if !initialized}
	<div class="min-h-screen flex items-center justify-center">
		<div class="text-center">
			<div class="animate-spin w-8 h-8 border-2 border-[var(--color-primary)] border-t-transparent rounded-full mx-auto mb-4"></div>
			<p class="text-gray-400">Loading...</p>
		</div>
	</div>
{:else}
	<!-- PWA Components -->
	{#if !$isOnline}
		<OfflineBanner />
	{/if}
	{#if $hasUpdate}
		<UpdatePrompt />
	{/if}
	<InstallPrompt />

	<slot />
{/if}
