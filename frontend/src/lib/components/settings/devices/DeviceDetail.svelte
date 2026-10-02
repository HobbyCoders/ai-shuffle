<script lang="ts">
	/**
	 * Every other device: a network worker, a worker that is joining, a
	 * paired phone or browser, a device known only to the account, or a
	 * legacy shared-database install. Revokes clear the account registration
	 * first (see DevicesModel.revoke).
	 */
	import { Cloud, Database, Globe, MonitorSmartphone, Server, Smartphone } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import { clientKind, clientPlatform, type DevicesModel } from './devices-model.svelte';

	interface Props {
		model: DevicesModel;
		/** `member:`, `join:`, `hub:`, `client:`, `account-worker:`, `account-device:` or `legacy:` key. */
		key: string;
	}

	let { model, key }: Props = $props();

	const member = $derived(key.startsWith('member:') ? model.members.find((m) => `member:${m.device_id}` === key) : undefined);
	const joining = $derived(model.joining.find((j) => j.key === key));
	const client = $derived(key.startsWith('client:') ? model.clients.find((c) => `client:${c.id}` === key) : undefined);
	const account = $derived(model.accountDevices.find((d) => d.key === key));
	const legacy = $derived(key.startsWith('legacy:') ? model.legacyInstalls.find((i) => `legacy:${i.id}` === key) : undefined);
	const linked = $derived(model.linkedRegistration(key));
	const busy = $derived(model.saving !== null);

	let rev = $state(0);

	async function rename(name: string) {
		if (await model.rename(key, name)) flashSaved();
		else rev += 1;
	}

	async function revoke(name: string, action: 'revoke' | 'remove' = 'revoke') {
		const ok = await model.revoke(key, name, action);
		if (ok && !key.startsWith('legacy:')) settingsNav.clearSelection('devices');
		if (ok && action === 'remove') settingsNav.clearSelection('devices');
	}

	function shortDate(iso: string | null | undefined): string | null {
		if (!iso) return null;
		const d = new Date(iso);
		return isNaN(d.getTime()) ? null : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function recent(iso: string | null | undefined): boolean {
		const t = iso ? new Date(iso).getTime() : NaN;
		return Number.isFinite(t) && model.now - t < 10 * 60 * 1000;
	}

	const alsoAccount = $derived(linked ? 'Also revokes account access' : undefined);
</script>

{#snippet nameRow(value: string, maxlength: number)}
	{#key rev}
		<SettingRow label="Name">
			<TextField {value} {maxlength} disabled={busy} label="Device name" onchange={rename} />
		</SettingRow>
	{/key}
{/snippet}

{#if member}
	<DetailPane
		name={member.name}
		icon={Server}
		status={member.online ? 'ok' : 'off'}
		statusText={member.online ? 'Online' : model.seen(member.last_seen)}
		badge="Worker"
		subtitle={member.platform ?? undefined}
		category="devices"
		item={key}
		facts={[
			{ label: 'Platform', value: member.platform },
			{ label: 'Version', value: member.version, mono: true },
			{ label: 'Seen', value: member.online ? 'Online' : model.seen(member.last_seen).replace(/^Seen /, '') },
			{ label: 'Network', value: model.status?.network?.name || 'Your network' }
		]}
		danger={[
			{
				label: 'Revoke this worker',
				hint: alsoAccount ?? 'Then rotate provider keys',
				button: 'Revoke',
				question: `Revoke ${member.name}?`,
				onConfirm: () => revoke(member.name),
				disabled: busy
			}
		]}
	>
		<RowGroup title="Device">
			{@render nameRow(member.name, 100)}
		</RowGroup>
	</DetailPane>
{:else if joining}
	<DetailPane
		name={joining.name}
		icon={Server}
		status="busy"
		statusText={joining.via === 'account' ? 'Joining through your account' : 'Joining with an invite'}
		badge="Worker"
		subtitle={joining.platform ?? undefined}
		category="devices"
		item={key}
		facts={[{ label: 'Platform', value: joining.platform }]}
	>
		<RowGroup title="Status">
			<SettingRow label="Waiting for {joining.name}" hint="Admitted on contact">
				<StatusDot tone="busy" />
			</SettingRow>
		</RowGroup>
	</DetailPane>
{:else if client}
	{@const kind = clientKind(client)}
	<DetailPane
		name={client.device_name}
		icon={kind === 'Phone' ? Smartphone : Globe}
		status={recent(client.last_seen) ? 'ok' : 'off'}
		statusText={model.seen(client.last_seen)}
		badge={kind === 'Device' ? 'Paired device' : kind}
		subtitle={clientPlatform(client) || undefined}
		category="devices"
		item={key}
		facts={[
			{ label: 'Kind', value: kind },
			{ label: 'Platform', value: clientPlatform(client) || null },
			{ label: 'Paired', value: shortDate(client.paired_at) },
			{ label: 'Seen', value: model.seen(client.last_seen).replace(/^Seen /, '') }
		]}
		danger={[
			{
				label: 'Revoke access',
				hint: alsoAccount ?? 'It must pair again',
				button: 'Revoke',
				question: `Revoke ${client.device_name}?`,
				onConfirm: () => revoke(client.device_name),
				disabled: busy
			}
		]}
	/>
{:else if account}
	<DetailPane
		name={account.name}
		icon={account.kind === 'Worker' ? Server : account.kind === 'Phone' ? Smartphone : account.kind === 'Browser' ? Globe : Cloud}
		status="off"
		statusText="Availability unknown"
		badge="On your account"
		subtitle={account.platform}
		category="devices"
		item={key}
		facts={[
			{ label: 'Kind', value: account.kind },
			{ label: 'Platform', value: account.platform }
		]}
		danger={account.current
			? []
			: [
					{
						label: 'Revoke from account',
						hint: account.kind === 'Worker' ? 'Loses remote access' : 'It must sign in again',
						button: 'Revoke',
						question: `Revoke ${account.name}?`,
						onConfirm: () => revoke(account.name),
						disabled: busy
					}
				]}
	/>
{:else if legacy}
	{@const approved = legacy.approved !== false}
	{@const actions: DangerAction[] = [
		...(approved
			? [
					{
						label: 'Revoke this worker',
						hint: alsoAccount ?? 'It stops getting tasks',
						button: 'Revoke',
						question: `Revoke ${legacy.device_name}?`,
						onConfirm: () => revoke(legacy.device_name),
						disabled: busy
					}
				]
			: []),
		{
			label: 'Remove from list',
			hint: 'It may reappear later',
			button: 'Remove',
			question: `Remove ${legacy.device_name}?`,
			onConfirm: () => revoke(legacy.device_name, 'remove'),
			disabled: busy
		}
	]}
	<DetailPane
		name={legacy.device_name}
		icon={Database}
		status={approved ? (legacy.online ? 'ok' : 'off') : 'warn'}
		statusText={approved ? (legacy.online ? 'Online' : model.seen(legacy.last_seen)) : 'Not approved'}
		badge="Shared database"
		subtitle="Legacy install"
		category="devices"
		item={key}
		facts={[
			{ label: 'Platform', value: legacy.platform },
			{ label: 'Version', value: legacy.version, mono: true },
			{ label: 'Seen', value: legacy.online ? 'Online' : model.seen(legacy.last_seen).replace(/^Seen /, '') }
		]}
		danger={actions}
	>
		{#snippet primary()}
			{#if !approved}
				<button type="button" class="st-btn primary" disabled={busy} onclick={() => model.approveLegacy(legacy, true)}>
					Allow
				</button>
			{/if}
		{/snippet}
		{#if approved}
			<RowGroup title="Device">
				{@render nameRow(legacy.device_name, 60)}
			</RowGroup>
		{:else}
			<RowGroup title="Device">
				<SettingRow label="Runs tasks" hint="Allow it first">
					<ValueText value="Not yet" />
				</SettingRow>
			</RowGroup>
		{/if}
	</DetailPane>
{:else}
	<EmptyDetail icon={MonitorSmartphone} title="Not here any more" message="This device left the list." />
{/if}
