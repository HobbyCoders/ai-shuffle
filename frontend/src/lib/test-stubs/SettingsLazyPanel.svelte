<script lang="ts">
 let { label, props = {} }: { label: string; props?: { initialSelectedId?: string | null } } = $props();
</script>
<section aria-label={`${label} panel`} data-initial-selected-id={props.initialSelectedId ?? ''}></section>
