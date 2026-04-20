<script lang="ts">
	import '@fontsource/playfair-display/700.css';
	import '@fontsource/playfair-display/900.css';
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { auth, isAuthenticated, setupRequired, claudeAuthenticated, onboardingComplete } from '$lib/stores/auth';
	import { theme } from '$lib/stores/theme';
	import { pwa, hasUpdate, isOnline } from '$lib/stores/pwa';
	import { chatPreferences } from '$lib/stores/chatPreferences';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import { files } from '$lib/stores/files';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import { hydrateModelRegistry } from '$lib/constants/models';
	import { onDestroy } from 'svelte';
	import type { Snippet } from 'svelte';
	import InstallPrompt from '$lib/components/InstallPrompt.svelte';
	import UpdatePrompt from '$lib/components/UpdatePrompt.svelte';
	import OfflineBanner from '$lib/components/OfflineBanner.svelte';
	import ClaudeStatusBanner from '$lib/components/ClaudeStatusBanner.svelte';
	import { claudeStatus } from '$lib/stores/claude-status';

	let { children }: { children: Snippet } = $props();

	let initialized = $state(false);

	// Auth poll: detects gh login/logout happening in a terminal.
	// Auth changes are rare, so 2-minute polling is plenty.
	// Pauses entirely when tab is hidden; checks immediately on tab return.
	const AUTH_POLL_INTERVAL_MS = 120_000;
	let authPollTimer: ReturnType<typeof setInterval> | null = null;

	function startAuthPoll() {
		if (authPollTimer) return;
		authPollTimer = setInterval(async () => {
			try {
				await auth.checkAuth();
			} catch (e) {
				console.debug('Auth poll failed:', e);
			}
		}, AUTH_POLL_INTERVAL_MS);
	}

	function stopAuthPoll() {
		if (authPollTimer) {
			clearInterval(authPollTimer);
			authPollTimer = null;
		}
	}

	function handleAuthVisibility() {
		if (document.hidden) {
			stopAuthPoll();
		} else {
			// Check immediately on tab return, then resume interval
			auth.checkAuth().catch(() => {});
			startAuthPoll();
		}
	}

	onMount(async () => {
		// Initialize theme on mount (applies from localStorage immediately to prevent flash)
		theme.init();

		// Initialize PWA features
		pwa.init();

		try {
			await auth.checkAuth();

			// Load and sync user preferences after auth is verified
			if ($isAuthenticated) {
				// Sync all settings from server (database is source of truth)
				// These run in parallel for faster startup
				await Promise.all([
					chatPreferences.load(),
					hotkeyPreferences.syncFromServer(),
					theme.syncFromServer(),
					files.syncFromServer(),
					gitActionPreferences.syncFromServer(),
					timezonePreference.syncFromServer(),
					hydrateModelRegistry()
				]);
			}

			initialized = true;

			// Handle redirects
			const path = $page.url.pathname;

			if ($setupRequired && path !== '/setup') {
				goto('/setup');
			} else if ($isAuthenticated && !$onboardingComplete && path !== '/setup') {
				goto('/setup');
			} else if (!$isAuthenticated && !$setupRequired && path !== '/login') {
				goto('/login');
			} else if ($isAuthenticated && path === '/login') {
				goto('/');
			} else if ($isAuthenticated && path === '/setup' && $onboardingComplete) {
				goto('/');
			}
		} catch (e) {
			console.error('Auth check failed:', e);
			initialized = true;
		}

		// Start visibility-aware auth polling
		startAuthPoll();
		document.addEventListener('visibilitychange', handleAuthVisibility);

		// Start Claude status polling (only when authenticated)
		if ($isAuthenticated) {
			claudeStatus.start();
		}
	});

	onDestroy(() => {
		stopAuthPoll();
		claudeStatus.stop();
		document.removeEventListener('visibilitychange', handleAuthVisibility);
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
	<ClaudeStatusBanner />
	{#if !$isOnline}
		<OfflineBanner />
	{/if}
	{#if $hasUpdate}
		<UpdatePrompt />
	{/if}
	<InstallPrompt />

	{@render children?.()}
{/if}
