<script lang="ts">
 let { label, props = {} }: { label: string; props?: { onDetailChange?: (open: boolean) => void } } = $props();
</script>
<section aria-label={`${label} panel`}>
 <button onclick={() => props.onDetailChange?.(true)}>Open editor</button>
 <textarea aria-label="Unsaved configuration"></textarea>
 <button onclick={() => props.onDetailChange?.(false)}>Close editor</button>
</section>
