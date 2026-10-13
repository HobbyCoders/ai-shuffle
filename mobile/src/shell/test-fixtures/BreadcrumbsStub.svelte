<script lang="ts">
	/** Test double for $lib SessionBreadcrumbs (mobile: the trail only, as the real one). */
	let { title, sessionId, projectLabel, mobile = false }: { title: string; sessionId: string; projectLabel: string; mobile?: boolean } = $props();
</script>

<nav aria-label="Breadcrumbs" data-session-id={sessionId} data-mobile={mobile} data-title={title}><span>{projectLabel}</span></nav>
