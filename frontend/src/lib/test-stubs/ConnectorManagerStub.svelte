<script lang="ts">
	/** Stand-in for ConnectorManager: shows which connector type it manages. */
	let { typeFilter = '' }: { typeFilter?: string; onChanged?: () => void } = $props();
</script>

<section aria-label="Accounts for {typeFilter}" data-type-filter={typeFilter}></section>
