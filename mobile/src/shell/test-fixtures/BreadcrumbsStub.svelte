<script lang="ts">
	/** Test double for $lib SessionBreadcrumbs. */
	let { title, sessionId, projectLabel, mobile = false }: { title: string; sessionId: string; projectLabel: string; mobile?: boolean } = $props();
</script>

<nav aria-label="Breadcrumbs" data-session-id={sessionId} data-mobile={mobile}><span>{projectLabel}</span> / <span>{title}</span></nav>
