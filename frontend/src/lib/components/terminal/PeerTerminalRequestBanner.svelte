<script lang="ts">
	/**
	 * App-wide nudge on a worker where a device is waiting to link (#674), so
	 * the owner sees it without opening Terminal first. Review opens the
	 * Terminal page, where the code from the device is entered; this banner
	 * never links anything itself.
	 *
	 * Only for the owner, and only polls when this worker has terminals on
	 * other workers turned on (its targets list another worker). Off by
	 * default: nothing is requested or drawn.
	 */
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { terminals } from '$lib/stores/terminals.svelte';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import { isPeerTarget, localWorkerId } from '$lib/terminal/peer';
	import Banner from '../Banner.svelte';
	import NotificationSlot from '../NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	const capability = $derived(terminals.capability);
	const enabled = $derived($isAdmin && capability.status === 'ready'
		&& (capability.targets ?? []).some((t) => isPeerTarget(t, localWorkerId(capability.targets))));

	$effect(() => { if ($isAdmin) void terminals.ensureCapability(); });
	$effect(() => { if (enabled) return peerTerminals.watch(); });

	const pending = $derived(enabled ? peerTerminals.requests : []);
	const onTerminalPage = $derived($layout.activeView?.type === 'terminal');
	/** Dismissed request ids: a new request shows again. */
	let dismissed = $state<string[]>([]);
	const shown = $derived(pending.filter((r) => !dismissed.includes(r.request_id)));
	const first = $derived(shown[0]);
</script>

{#if first && !onTerminalPage}
	<NotificationSlot order={DOCK_ORDER.banners}>
		<Banner
			variant="page"
			severity="warning"
			title={shown.length > 1 ? `${shown.length} devices want to link for terminals here` : `${first.label || 'A device'} wants to link for terminals here`}
			message="Read the code on that device and enter it here to link it. The request runs out in ten minutes."
			action={{ label: 'Review', onClick: () => layout.openFeaturePage('terminal') }}
			dismissible
			onDismiss={() => (dismissed = [...dismissed, ...shown.map((r) => r.request_id)])}
		/>
	</NotificationSlot>
{/if}
