<script lang="ts">
	/** Test double for $lib SessionBreadcrumbs. */
	let { title, sessionId, projectLabel }: { title: string; sessionId: string; projectLabel: string; mobile?: boolean } = $props();
</script>

<nav aria-label="Breadcrumbs" data-session-id={sessionId}><span>{projectLabel}</span> / <span>{title}</span></nav>
