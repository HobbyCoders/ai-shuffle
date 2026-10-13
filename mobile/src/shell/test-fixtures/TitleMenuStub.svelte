<script lang="ts">
	/** Test double for $lib SessionTitleMenu: shows which props reached the chat-actions chevron. */
	let { title, sessionId = null, variant = 'crumb', projectLabel = null }: { title: string; sessionId?: string | null; variant?: string; projectLabel?: string | null } = $props();
</script>

<button type="button" aria-label="Chat actions" data-variant={variant} data-session-id={sessionId} data-title={title} data-project={projectLabel}></button>
