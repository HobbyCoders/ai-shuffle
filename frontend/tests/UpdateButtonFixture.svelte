<script lang="ts">
	import DesktopUpdateButton from '../src/lib/components/layout/sidebar/DesktopUpdateButton.svelte';
</script>

<!-- A sidebar-header-sized strip: the button sits where Sidebar.svelte puts it. -->
<header class="strip">
	<span class="brand">AI Shuffle</span>
	<DesktopUpdateButton />
</header>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.strip { display: flex; align-items: center; gap: 8px; width: 260px; max-width: 100%; padding: 10px 12px; box-sizing: border-box; border-bottom: 1px solid var(--border); background: var(--sidebar, var(--background)); }
	.brand { flex: 1; font-size: 13px; font-weight: 600; }
</style>
