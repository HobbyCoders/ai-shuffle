<script lang="ts">
	let { onClose }: { onClose: () => void } = $props();
</script>

<button type="button" onclick={onClose}>Complete provider connection</button>
