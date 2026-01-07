<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { auth, isAuthenticated, setupRequired, claudeAuthenticated } from '$lib/stores/auth';

	let initialized = false;

	onMount(async () => {
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
</svelte:head>

{#if !initialized}
	<div class="min-h-screen flex items-center justify-center">
		<div class="text-center">
			<div class="animate-spin w-8 h-8 border-2 border-[var(--color-primary)] border-t-transparent rounded-full mx-auto mb-4"></div>
			<p class="text-gray-400">Loading...</p>
		</div>
	</div>
{:else}
	<slot />
{/if}
