<script lang="ts">
	/**
	 * App-wide nudge on the worker that would run a peer shell (#196): another
	 * worker asked for a terminal here and it waits for this owner. A request
	 * lasts two minutes, so it must be seen without opening Terminal first.
	 * Review opens the Terminal page, where the code is compared and the
	 * request approved or denied; this banner never approves anything itself.
	 *
	 * Only for the owner, and only polls when this worker has peer terminals
	 * turned on (its targets list another worker). Off by default: nothing
	 * is requested or drawn.
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

	const pending = $derived(enabled ? peerTerminals.requests.filter((r) => r.status === 'pending') : []);
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
			title={shown.length > 1 ? `${shown.length} workers want a terminal here` : `${first.source_name} wants a terminal here`}
			message="Compare the six-character code on both workers before you approve. The request expires in two minutes."
			action={{ label: 'Review', onClick: () => layout.openFeaturePage('terminal') }}
			dismissible
			onDismiss={() => (dismissed = [...dismissed, ...shown.map((r) => r.request_id)])}
		/>
	</NotificationSlot>
{/if}
