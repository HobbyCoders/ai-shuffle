<script lang="ts">
	/**
	 * One saved sign-in, or the draft from "Add sign-in". Edits stay in the form
	 * until Save. Stored secrets are never loaded: a secret field left blank
	 * keeps what is stored, and anything typed is dropped as soon as it is sent.
	 */
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import NumberField from '../rows/NumberField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import {
		DRAFT_ID,
		KIND_OPTIONS,
		emptyForm,
		kindIcon,
		kindLabel,
		secretLabel,
		type VaultKind,
		type VaultModel
	} from './vault-model.svelte';

	interface Props {
		model: VaultModel;
		/** A sign-in id, or DRAFT_ID. */
		id: string;
	}

	let { model, id }: Props = $props();

	const CATEGORY = 'vault' as const;
	const METHOD_OPTIONS = [
		{ value: 'password', label: 'Password' },
		{ value: 'key', label: 'Private key' }
	] as const;
	const SERVICE_METHOD_OPTIONS = [
		{ value: 'token', label: 'API token' },
		{ value: 'password', label: 'Username and password' }
	] as const;

	// Forms are created outside rendering; the collection keys this component by id.
	// svelte-ignore state_referenced_locally
	model.ensure(id);

	const isDraft = $derived(id === DRAFT_ID);
	const entry = $derived(isDraft ? undefined : model.entry(id));
	const form = $derived(model.form(id));
	const started = $derived(isDraft ? undefined : model.started(id));
	const dirty = $derived(model.dirty(id));
	const saving = $derived(model.saving === id);
	const errors = $derived(model.errors[id] ?? []);
	const notice = $derived(model.notices[id]);
	const locked = $derived(!model.editable);
	/** The stored secret is replaced only when something is typed. */
	const keeps = $derived(!isDraft);
	/** Changing more than the name ends access that uses this sign-in. */
	const endsAccess = $derived(
		!!form && !!started && JSON.stringify({ ...form, label: '' }) !== JSON.stringify({ ...started, label: '' })
	);

	async function save() {
		const saved = await model.save(id);
		if (!saved) return;
		flashSaved();
		if (isDraft) settingsNav.replaceItem(saved, CATEGORY);
	}

	function cancelDraft() {
		model.discardDraft();
		settingsNav.clearSelection(CATEGORY);
	}

	async function remove() {
		if (await model.remove(id)) settingsNav.clearSelection(CATEGORY);
	}

	function setKind(kind: VaultKind) {
		if (!form || form.kind === kind) return;
		// A new kind starts clean, so a secret typed for one never reaches the other.
		Object.assign(form, emptyForm(kind), { label: form.label });
	}

	function setMethod(method: string) {
		if (!form) return;
		form.useKey = method === 'key';
		form.password = '';
		form.privateKey = '';
		form.passphrase = '';
	}

	function setServiceMethod(method: string) {
		if (!form || form.basic === (method === 'password')) return;
		form.basic = method === 'password';
		// What was typed for one way of signing in never becomes the other.
		form.secret = '';
		const same = started && started.basic === form.basic ? started : undefined;
		form.username = form.basic ? (same?.username ?? '') : '';
		form.authHeader = form.basic ? 'Authorization' : (same?.authHeader ?? 'Authorization');
		form.authScheme = form.basic ? 'Basic' : (same?.authScheme ?? 'Bearer');
	}
</script>

{#if form}
	<DetailPane
		name={isDraft ? form.label.trim() || 'New sign-in' : (entry?.label ?? form.label)}
		icon={kindIcon(form.kind)}
		status={isDraft ? 'busy' : dirty ? 'warn' : undefined}
		statusText={isDraft ? 'Not added yet' : dirty ? 'Unsaved changes' : undefined}
		badge={kindLabel(form.kind)}
		subtitle={entry?.target}
		category={CATEGORY}
		item={id}
		facts={entry
			? [
					{ label: 'Kind', value: kindLabel(entry.kind) },
					{ label: 'Signs in with', value: secretLabel(entry) },
					{
						label: entry.kind === 'service' ? 'Certificate' : 'Host key',
						value: entry.fingerprint ? 'Pinned' : entry.kind === 'service' ? 'System trust' : 'Pinned at next use'
					}
				]
			: []}
		danger={isDraft || !entry
			? []
			: [
					{
						label: 'Remove this sign-in',
						hint: 'Ends access that uses it',
						button: 'Remove',
						question: `Remove ${entry.label}?`,
						onConfirm: remove,
						disabled: model.forbidden || !!model.blocked
					}
				]}
	>
		{#snippet primary()}
			{#if isDraft}
				<button type="button" class="st-btn ghost" onclick={cancelDraft}>Cancel</button>
				<button type="button" class="st-btn primary" disabled={saving || locked} onclick={save}>
					{saving ? 'Adding…' : 'Add sign-in'}
				</button>
			{:else}
				{#if dirty}
					<button type="button" class="st-btn ghost" disabled={saving} onclick={() => model.discard(id)}>Discard</button>
				{/if}
				<button type="button" class="st-btn primary" disabled={!dirty || saving || locked} onclick={save}>
					{saving ? 'Saving…' : 'Save'}
				</button>
			{/if}
		{/snippet}
		{#snippet banner()}
			{#if model.changedElsewhere[id]}
				<InlineBanner tone="warn">
					Changed elsewhere, your edits kept
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.discard(id)}>Load latest</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{#if errors.length}
				<InlineBanner tone="warn">
					<span role="alert">{errors.join('. ')}</span>
				</InlineBanner>
			{:else if notice}
				<InlineBanner tone="info">
					<span role="status">{notice}</span>
				</InlineBanner>
			{:else if endsAccess}
				<InlineBanner tone="info">Saving this ends access that uses the sign-in. Chats ask you again.</InlineBanner>
			{/if}
		{/snippet}
		{#snippet children()}
			<RowGroup title="Sign-in">
				{#if isDraft}
					<SettingRow label="Kind">
						<Segmented options={KIND_OPTIONS} value={form.kind} onchange={(v) => setKind(v as VaultKind)} size="sm" />
					</SettingRow>
				{/if}
				<SettingRow label="Name" hint="Shown when a chat asks">
					<TextField bind:value={form.label} label="Name" placeholder={form.kind === 'service' ? 'Cloudflare' : 'HomeLab'} maxlength={80} width={260} />
				</SettingRow>
				{#if form.kind === 'machine'}
					<SettingRow label="Address" hint="IP or hostname">
						<TextField bind:value={form.host} label="Address" placeholder="10.0.0.251" mono maxlength={253} width={260} />
					</SettingRow>
					<SettingRow label="Port">
						<NumberField bind:value={form.port} label="Port" min={1} max={65535} />
					</SettingRow>
					<SettingRow label="Username">
						<TextField bind:value={form.username} label="Username" placeholder="root" mono maxlength={128} autocomplete="off" width={260} />
					</SettingRow>
				{:else}
					<SettingRow label="Address" hint="Starts with https://">
						<TextField bind:value={form.url} label="Address" type="url" placeholder="https://api.example.com" mono maxlength={2048} width={300} />
					</SettingRow>
					<SettingRow label="Allow changes" hint="Off: read-only requests">
						<Switch bind:checked={form.writable} label="Allow changes" />
					</SettingRow>
				{/if}
			</RowGroup>

			{#if form.kind === 'machine'}
				<RowGroup title="Secret">
					<SettingRow label="Signs in with">
						<Segmented options={METHOD_OPTIONS} value={form.useKey ? 'key' : 'password'} onchange={setMethod} size="sm" />
					</SettingRow>
					{#if form.useKey}
						<SettingRow label="Private key" hint={keeps && started?.useKey ? 'Blank keeps the stored key' : 'Paste the whole key'}>
							{#snippet expanded()}
								<textarea
									class="st-area vault-key"
									aria-label="Private key"
									spellcheck="false"
									autocomplete="off"
									autocapitalize="off"
									placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
									bind:value={form.privateKey}
								></textarea>
							{/snippet}
						</SettingRow>
						<SettingRow label="Key passphrase" hint="Blank when the key has none">
							<TextField bind:value={form.passphrase} label="Key passphrase" type="password" autocomplete="new-password" mono width={260} />
						</SettingRow>
					{:else}
						<SettingRow label="Password" hint={keeps && !started?.useKey ? 'Blank keeps the stored one' : undefined}>
							<TextField
								bind:value={form.password}
								label="Password"
								type="password"
								autocomplete="new-password"
								placeholder={keeps && !started?.useKey ? 'Unchanged' : ''}
								mono
								width={260}
							/>
						</SettingRow>
					{/if}
				</RowGroup>
				<RowGroup title="Host key">
					{#if entry}
						<SettingRow label="Pinned key" hint="Checked at every connection">
							<ValueText value={entry.fingerprint || 'Pinned at the next connection you approve'} mono={!!entry.fingerprint} />
						</SettingRow>
					{/if}
					<SettingRow
						label={entry ? 'Replace with' : 'Fingerprint'}
						hint={entry ? 'Blank keeps the pinned key' : 'Blank pins at first use'}
						help="A host-key fingerprint looks like SHA256: followed by 43 characters."
					>
						<TextField bind:value={form.fingerprint} label="Host key fingerprint" placeholder="SHA256:…" mono maxlength={100} width={300} />
					</SettingRow>
				</RowGroup>
			{:else}
				<RowGroup title="Secret">
					<SettingRow label="Signs in with">
						<Segmented options={SERVICE_METHOD_OPTIONS} value={form.basic ? 'password' : 'token'} onchange={setServiceMethod} size="sm" />
					</SettingRow>
					{#if form.basic}
						<SettingRow label="Username" hint="Sent with the password as HTTP Basic">
							<TextField bind:value={form.username} label="Username" mono maxlength={128} autocomplete="off" width={260} />
						</SettingRow>
						<SettingRow label="Password" hint={keeps && started?.basic ? 'Blank keeps the stored one' : undefined}>
							<TextField
								bind:value={form.secret}
								label="Password"
								type="password"
								autocomplete="new-password"
								placeholder={keeps && started?.basic ? 'Unchanged' : ''}
								mono
								width={260}
							/>
						</SettingRow>
					{:else}
						<SettingRow label="Header" hint="Carries the secret">
							<TextField bind:value={form.authHeader} label="Header" placeholder="Authorization" mono maxlength={64} width={220} />
						</SettingRow>
						<SettingRow label="Scheme" hint="Blank sends the secret alone">
							<TextField bind:value={form.authScheme} label="Scheme" placeholder="Bearer" mono maxlength={32} width={220} />
						</SettingRow>
						<SettingRow label="API key or token" hint={keeps && !started?.basic ? 'Blank keeps the stored one' : undefined}>
							<TextField
								bind:value={form.secret}
								label="API key or token"
								type="password"
								autocomplete="new-password"
								placeholder={keeps && !started?.basic ? 'Unchanged' : ''}
								mono
								width={260}
							/>
						</SettingRow>
					{/if}
				</RowGroup>
				<RowGroup title="Certificate">
					{#if entry?.fingerprint}
						<SettingRow label="Pinned certificate" hint="Replaces system trust">
							<ValueText value={entry.fingerprint} mono />
						</SettingRow>
					{/if}
					<SettingRow
						label={entry?.fingerprint ? 'Replace with' : 'Pin a certificate'}
						hint="Optional"
						help="The certificate's SHA-256 fingerprint: 64 hex digits. Leave blank to trust certificates the system trusts."
					>
						<TextField bind:value={form.fingerprint} label="Certificate fingerprint" placeholder="SHA-256, 64 hex digits" mono maxlength={100} width={300} />
					</SettingRow>
				</RowGroup>
			{/if}
			<p class="st-note">
				Encrypted on this worker. The agent never sees a sign-in; using one still needs your approval in the chat that asks for it.
			</p>
		{/snippet}
	</DetailPane>
{/if}

<style>
	:global(.settings-shell) textarea.st-area.vault-key {
		min-height: 120px;
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 12px;
		/* A pasted key is a secret: do not show it as readable text. */
		-webkit-text-security: disc;
	}
</style>
