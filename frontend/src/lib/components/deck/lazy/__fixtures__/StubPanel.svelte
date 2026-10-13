<script lang="ts">
	/** Test fixture for LazyPanel — stands in for a real feature surface. */
	let { label = 'stub' }: { label?: string } = $props();
</script>

<div data-testid="stub-panel">{label}</div>
