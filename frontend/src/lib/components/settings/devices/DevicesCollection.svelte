<script lang="ts">
	/**
	 * Devices: the Shuffle account, approval requests, pairing codes, this
	 * worker, the other workers, phones and browsers, devices known only to
	 * the account, and legacy shared-database installs. "Add device" makes a
	 * pairing code item.
	 */
	import { onMount } from 'svelte';
	import {
		CircleUser,
		Cloud,
		Database,
		Globe,
		MonitorSmartphone,
		QrCode,
		Server,
		ShieldQuestion,
		Smartphone
	} from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import type { SettingsIcon } from '$lib/settings/categories';
	import type { StatusTone } from '$lib/settings/types';
	import { NETWORK_STATE_LABELS } from '$lib/api/network-status';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import AccountDetail from './AccountDetail.svelte';
	import RequestDetail from './RequestDetail.svelte';
	import PairingDetail from './PairingDetail.svelte';
	import ThisWorkerDetail from './ThisWorkerDetail.svelte';
	import DeviceDetail from './DeviceDetail.svelte';
	import { clientKind, clientPlatform, devicesModel, type DevicesModel } from './devices-model.svelte';
	import { requestExpired, requestId, requestStatusText } from './device-requests.svelte';
	import { KIND_NAME } from './pairing-codes.svelte';
	import { accountStatus } from './device-status';

	interface Props {
		/** Tests pass their own model. */
		model?: DevicesModel;
	}

	let { model = devicesModel }: Props = $props();

	const CATEGORY = 'devices' as const;
	const RECENT_MS = 10 * 60 * 1000;
	let filter = $state('');

	onMount(() => model.start());

	interface Row {
		id: string;
		name: string;
		summary: string;
		status: StatusTone;
		icon: SettingsIcon;
	}

	const selfTone: Record<string, StatusTone> = {
		ready: 'ok',
		syncing: 'busy',
		waiting: 'busy',
		'needs-setup': 'warn',
		offline: 'err'
	};

	function recent(iso: string | null | undefined): boolean {
		if (!iso) return false;
		const t = new Date(iso).getTime();
		return Number.isFinite(t) && model.now - t < RECENT_MS;
	}

	const groups = $derived.by((): { label: string; rows: Row[] }[] => {
		const acct = accountStatus(model.account);
		const list: { label: string; rows: Row[] }[] = [
			{
				label: 'Account',
				rows: [{ id: 'account', name: 'Shuffle account', summary: acct.summary, status: acct.tone, icon: CircleUser }]
			},
			{
				label: 'Requests',
				rows: model.requests.devices.map((d) => {
					const expired = requestExpired(d, model.now);
					const pending = !d.revoked_at && d.confirmation === 'pending';
					return {
						id: requestId(d),
						name: d.device_name || 'Unnamed device',
						summary: pending && expired ? 'Key expired, pair again' : requestStatusText(d),
						status: d.revoked_at
							? 'off'
							: pending
								? expired
									? 'off'
									: 'warn'
								: d.confirmation === 'approved'
									? 'ok'
									: 'err',
						icon: ShieldQuestion
					};
				})
			},
			{
				label: 'Pairing codes',
				rows: model.pairing.items.map((item) => ({
					id: item.id,
					name: KIND_NAME[item.kind],
					summary: item.grant
						? `Works once, ${model.pairing.countdown(item)} left`
						: item.creating
							? 'Creating…'
							: item.expired
								? 'Expired'
								: item.error
									? 'Not created'
									: 'Not created yet',
					status: item.grant || item.creating ? 'busy' : 'off',
					icon: QrCode
				}))
			},
			{
				label: 'This worker',
				rows: model.status
					? [
							{
								id: 'self',
								name: model.workerName,
								summary: [
									model.status.platform,
									model.status.version,
									NETWORK_STATE_LABELS[model.uiState].toLowerCase()
								]
									.filter(Boolean)
									.join(', '),
								status: selfTone[model.uiState],
								icon: Server
							}
						]
					: []
			},
			{
				label: 'Workers',
				rows: [
					...model.members.map((m) => ({
						id: `member:${m.device_id}`,
						name: m.name,
						summary: [m.platform, m.online ? 'online' : model.seen(m.last_seen).toLowerCase()]
							.filter(Boolean)
							.join(', '),
						status: (m.online ? 'ok' : 'off') as StatusTone,
						icon: Server
					})),
					...model.joining.map((j) => ({
						id: j.key,
						name: j.name,
						summary: j.via === 'account' ? 'Joining through your account' : 'Joining with an invite',
						status: 'busy' as StatusTone,
						icon: Server
					}))
				]
			},
			{
				label: 'Phones and browsers',
				rows: model.clients.map((c) => ({
					id: `client:${c.id}`,
					name: c.device_name,
					summary: [clientKind(c), clientPlatform(c), model.seen(c.last_seen).toLowerCase()].filter(Boolean).join(', '),
					status: (recent(c.last_seen) ? 'ok' : 'off') as StatusTone,
					icon: clientKind(c) === 'Phone' ? Smartphone : Globe
				}))
			},
			{
				label: 'On your account',
				rows: model.accountDevices.map((d) => ({
					id: d.key,
					name: d.name,
					summary: `${d.kind}, availability unknown`,
					status: 'off' as StatusTone,
					icon: d.kind === 'Worker' ? Server : d.kind === 'Phone' ? Smartphone : d.kind === 'Browser' ? Globe : Cloud
				}))
			},
			{
				label: 'Shared database',
				rows: model.legacyInstalls.map((i) => ({
					id: `legacy:${i.id}`,
					name: i.device_name,
					summary: i.approved === false ? 'Not approved' : i.online ? 'Online' : model.seen(i.last_seen),
					status: (i.approved === false ? 'warn' : i.online ? 'ok' : 'off') as StatusTone,
					icon: Database
				}))
			}
		];
		const q = filter.trim().toLowerCase();
		return list
			.map((g) => ({
				label: g.label,
				rows: q ? g.rows.filter((r) => `${r.name} ${r.summary} ${g.label}`.toLowerCase().includes(q)) : g.rows
			}))
			.filter((g) => g.rows.length);
	});

	const count = $derived(
		model.complete
			? model.total
			: model.workersLoaded || model.clientsLoaded || model.workersError || model.clientsError
				? `${model.total} loaded`
				: ''
	);
	const selected = $derived(settingsNav.selectedItem(CATEGORY));

	async function add() {
		const id = await model.pairing.add();
		settingsNav.selectItem(id, CATEGORY);
	}
</script>

<CollectionPage category={CATEGORY} {count} addLabel="Add device" onAdd={add} bind:filter>
	{#snippet list()}
		{#if model.statusError}
			<InlineBanner tone="warn">
				<span role="alert">{model.statusError}</span>
				{#snippet actions()}<button type="button" class="st-btn sm" onclick={() => model.loadStatus()}>Retry</button
					>{/snippet}
			</InlineBanner>
		{/if}
		{#if model.workersError}
			<InlineBanner tone="warn">
				<span role="alert">Workers: {model.workersError}</span>
				{#snippet actions()}<button type="button" class="st-btn sm" onclick={() => model.loadWorkers()}
						>Retry workers</button
					>{/snippet}
			</InlineBanner>
		{/if}
		{#if model.clientsError}
			<InlineBanner tone="warn">
				<span role="alert">Phones and browsers: {model.clientsError}</span>
				{#snippet actions()}
					<button type="button" class="st-btn sm" onclick={() => model.loadClients()}>Retry phones and browsers</button>
				{/snippet}
			</InlineBanner>
		{/if}
		{#if model.stale && model.status}
			<InlineBanner tone="info">No answer from the worker. Showing saved data.</InlineBanner>
		{/if}
		{#if model.startupSyncNote}
			<InlineBanner tone="info">{model.startupSyncNote}</InlineBanner>
		{/if}
		{#if model.error}
			<InlineBanner tone="warn">
				<span role="alert">{model.error}</span>
				{#snippet actions()}<button type="button" class="st-btn ghost sm" onclick={() => (model.error = '')}
						>Dismiss</button
					>{/snippet}
			</InlineBanner>
		{/if}
		{#if model.forbidden}<p class="st-empty">{FORBIDDEN_TEXT}</p>{/if}
		{#if model.notice}<p class="notice" role="status">{model.notice}</p>{/if}

		{#each groups as group (group.label)}
			<ListGroupLabel label={group.label} />
			{#each group.rows as row (row.id)}
				<ListItem
					id={row.id}
					name={row.name}
					summary={row.summary}
					status={row.status}
					icon={row.icon}
					category={CATEGORY}
				/>
			{/each}
		{/each}
		{#if !model.clientsLoaded && !model.clientsError && !model.forbidden}
			<p class="st-empty" role="status">Loading devices…</p>
		{/if}
		{#if model.account.profile && model.account.profile.devices === undefined}
			<p class="st-empty">Update the account service to list its phones and browsers.</p>
		{/if}
		{#if filter.trim() && groups.length === 0}<p class="st-empty">Nothing matches.</p>{/if}
	{/snippet}
	{#snippet detail()}
		{@const request = model.requests.byId(selected)}
		{@const code = model.pairing.byId(selected)}
		{#if selected === 'account'}
			<AccountDetail flow={model.account} />
		{:else if request}
			<RequestDetail requests={model.requests} device={request} />
		{:else if code}
			<PairingDetail pairing={model.pairing} item={code} />
		{:else if selected === 'self' && model.status}
			<ThisWorkerDetail {model} />
		{:else if selected && selected !== 'self' && model.status !== null && selected.includes(':')}
			<DeviceDetail {model} key={selected} />
		{:else if model.forbidden && !model.status}
			<EmptyDetail icon={MonitorSmartphone} title="Devices" message={FORBIDDEN_TEXT} />
		{:else}
			<EmptyDetail icon={MonitorSmartphone} message="Select a device." />
		{/if}
	{/snippet}
</CollectionPage>

<style>
	.notice {
		margin: 6px 4px 4px;
		padding: 6px 10px;
		font-size: 12.5px;
		color: var(--st-mfg);
		border-radius: var(--st-radius-md);
		background: var(--st-muted);
	}
</style>
