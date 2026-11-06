<script lang="ts">
	import '@fontsource-variable/inter';
	import '@fontsource-variable/jetbrains-mono';
	import '../app.css';
	import type { Snippet } from 'svelte';
	import RootLayout from '$lib/components/layout/RootLayout.svelte';

	let { children }: { children: Snippet } = $props();
</script>

<RootLayout>
	{@render children()}
</RootLayout>
