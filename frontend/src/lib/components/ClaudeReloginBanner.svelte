<script lang="ts">
	/**
	 * One-time notice after the upgrade that retired the app's own Claude
	 * sign-in: sign out of the Claude Code CLI and sign in again, so its token
	 * comes from the CLI's own sign-in. Shown to the owner until dismissed (or
	 * until they sign out from the Claude Code CLI panel); the server keeps the
	 * dismissal per worker.
	 */
	import { auth, isAdmin } from '$lib/stores/auth.svelte';
	import { dismissClaudeReloginNotice } from '$lib/api/claude-cli';
	import Banner from './Banner.svelte';
	import NotificationSlot from './NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	let hidden = $state(false);
	const notice = $derived($auth.status?.claude_cli?.relogin_notice ?? null);

	async function dismiss() {
		hidden = true;
		try {
			await dismissClaudeReloginNotice();
		} catch {
			// Shown again on the next load; nothing else depends on it.
		}
	}
</script>

{#if notice && $isAdmin && !hidden}
	<NotificationSlot order={DOCK_ORDER.account}>
		<Banner
			variant="page"
			title="Sign in to the Claude Code CLI again"
			message={`${notice.message}`}
			severity="warning"
			dismissible
			onDismiss={dismiss}
		/>
	</NotificationSlot>
{/if}
