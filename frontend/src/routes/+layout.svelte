<script lang="ts">
	import '@fontsource-variable/inter';
	import '@fontsource-variable/jetbrains-mono';
	import '@fontsource/playfair-display/700.css';
	import '@fontsource/playfair-display/900.css';
	import '../app.css';
	import type { Snippet } from 'svelte';
	import RootLayout from '$lib/components/layout/RootLayout.svelte';

	let { children }: { children: Snippet } = $props();
</script>

<RootLayout>
	{@render children()}
</RootLayout>
