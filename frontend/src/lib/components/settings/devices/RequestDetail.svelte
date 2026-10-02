<script lang="ts">
	/**
	 * A device that signed in through the account and asks this worker to
	 * approve it. Approve only after the ID matches the one on the device.
	 */
	import { RefreshCw, ShieldQuestion } from 'lucide-svelte';
	import type { WorkerDeviceConfirmation } from '$lib/api/worker-device-confirmations';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import HelpTip from '../rows/HelpTip.svelte';
	import { requestExpired, requestId, requestStatusText, type DeviceRequests } from './device-requests.svelte';

	interface Props {
		requests: DeviceRequests;
		device: WorkerDeviceConfirmation;
	}

	let { requests, device }: Props = $props();

	const APPROVE_HELP =
		'Only approve a request you started. Compare this device ID with the one shown on the device; names are not proof. A LAN connection alone cannot approve.';

	const name = $derived(device.device_name || 'Unnamed device');
	const expired = $derived(requestExpired(device));
	const open = $derived(!device.revoked_at && device.confirmation !== 'denied');
	const pending = $derived(open && device.confirmation === 'pending');
	const blocked = $derived(requests.saving || Boolean(requests.error));
	const tone = $derived(
		device.revoked_at
			? 'off'
			: device.confirmation === 'pending'
				? expired
					? 'off'
					: 'warn'
				: device.confirmation === 'approved'
					? 'ok'
					: 'err'
	);
	const expires = $derived(
		device.api_key_expires_at
			? new Date(device.api_key_expires_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
					(expired ? ', expired' : '')
			: null
	);
</script>

<DetailPane
	{name}
	icon={ShieldQuestion}
	status={tone}
	statusText={pending ? (expired ? 'Key expired' : 'Awaiting your approval') : requestStatusText(device)}
	badge="Access request"
	subtitle={pending ? 'Wants to be approved' : undefined}
	category="devices"
	item={requestId(device)}
	dangerRows={open ? denyRow : undefined}
	facts={[
		{ label: 'Device ID', value: device.hub_device_id || device.device_id, mono: true },
		{ label: 'Platform', value: device.platform || 'Unknown' },
		{ label: 'Key expires', value: expires }
	]}
>
	{#snippet primary()}
		{#if pending}
			<InlineConfirm
				label="Approve"
				question="IDs match?"
				tone="primary"
				variant="primary"
				disabled={blocked || expired}
				bind:armed={() => requests.isArmed(device, true), (v) => (v ? requests.arm(device, true) : requests.disarm())}
				onConfirm={() => requests.decide()}
			/>
		{/if}
	{/snippet}
	{#snippet banner()}
		{#if requests.error}
			<InlineBanner tone="warn">
				<span role="alert">{requests.error} Use this worker or an approved device, then refresh.</span>
			</InlineBanner>
		{/if}
		{#if requests.notice}
			<InlineBanner tone="info"><span role="status">{requests.notice}</span></InlineBanner>
		{/if}
	{/snippet}

	<RowGroup title="Check the ID">
		{#snippet actions()}<HelpTip text={APPROVE_HELP} about="Approving devices" />{/snippet}
		<SettingRow label="ID shown on the device">
			<ValueText value={device.hub_device_id || device.device_id} mono tone="fg" />
		</SettingRow>
		<SettingRow label="Approve from" hint="LAN alone cannot approve">
			<ValueText value="This worker or an approved device" />
		</SettingRow>
		{#if device.confirmation === 'denied'}
			<SettingRow label="Denied for good" hint="Pair a new identity">
				<ValueText value="Cannot be approved again" />
			</SettingRow>
		{/if}
		<SettingRow label="Requests">
			<button
				type="button"
				class="st-btn"
				disabled={requests.loading || requests.saving}
				onclick={() => requests.refresh()}
			>
				<RefreshCw />{requests.loading ? 'Refreshing…' : 'Refresh requests'}
			</button>
		</SettingRow>
	</RowGroup>
</DetailPane>

{#snippet denyRow()}
	<SettingRow
		label={device.confirmation === 'approved' ? 'Revoke approval' : 'Deny this request'}
		hint={device.confirmation === 'approved' ? 'Stops its sessions here' : 'Final for this identity'}
	>
		<InlineConfirm
			label={device.confirmation === 'approved' ? 'Revoke' : 'Deny'}
			question="{device.confirmation === 'approved' ? 'Revoke' : 'Deny'} {name}?"
			disabled={blocked}
			bind:armed={() => requests.isArmed(device, false), (v) => (v ? requests.arm(device, false) : requests.disarm())}
			onConfirm={() => requests.decide()}
		/>
	</SettingRow>
{/snippet}
