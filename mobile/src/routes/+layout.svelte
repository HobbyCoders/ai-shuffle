<script lang="ts">
	import '@fontsource-variable/inter';
	import '@fontsource-variable/jetbrains-mono';
	import '@fontsource/playfair-display/700.css';
	import '@fontsource/playfair-display/900.css';
	import '../app.css';
	import type { Snippet } from 'svelte';
	import { page } from '$app/stores';
	import { installMobileShell } from '../shell/register';
	import RootLayout from '$lib/components/layout/RootLayout.svelte';

	// Before anything renders: the platform store says mobile and the shared
	// composer gets the mobile collapse / gesture / sheet behaviour.
	installMobileShell();

	let { children }: { children: Snippet } = $props();
</script>

<!-- Public, explicit sample route: never bootstrap worker auth, sync, or API clients. -->
{#if $page.url.pathname.replace(/\/$/, '').endsWith('/demo')}
	{@render children()}
{:else}
	<RootLayout mobileApp>
		{@render children()}
	</RootLayout>
{/if}
