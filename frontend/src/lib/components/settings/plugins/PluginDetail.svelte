<script lang="ts" module>
	const FORMAT_LABELS: Record<string, string> = {
		claude: 'Claude plugin',
		'agent-plugins': 'Agent plugin',
		shuffle: 'Connector runtime'
	};
</script>

<script lang="ts">
	/**
	 * PluginDetail: one agent plugin package. Installed: enable switch,
	 * overview (what it includes, outside services it uses with a sign-in per
	 * Connections row, what was adapted
	 * for this app, what does not load here, README, install location),
	 * contents, uninstall. Not installed: Install, which leaves it switched off.
	 *
	 * Switching on a plugin that runs code (hooks, MCP servers) first shows
	 * what will run, with one confirm. A plugin an agent wrote or installed
	 * opens on Review: everything it runs and adds, then Approve and enable
	 * (an authored draft pins the reviewed content hash).
	 */
	import { tick, untrack } from 'svelte';
	import { ArrowUpCircle, Download, ShieldAlert } from 'lucide-svelte';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import { getPluginsModel, pluginItem } from './plugins-model.svelte';
	import {
		NEEDS_REVIEW,
		adaptedParts,
		componentLabel,
		declaredServices,
		leftOutParts,
		runsCode
	} from './plugin-parts';
	import { PACKAGE_ICON, REVIEW_ICON } from './icons';
	import { renderReadme } from './readme';
	import RunsCodeList from './RunsCodeList.svelte';
	import PartNotes from './PartNotes.svelte';
	import PluginConnections from './PluginConnections.svelte';

	interface Props {
		/** Plugin id, e.g. `toolkit@mp` or `release-notes@authored`. */
		id: string;
	}

	let { id }: Props = $props();
	const model = getPluginsModel();
	const item = $derived(pluginItem(id));
	const entry = $derived(model.entryByItem(item));
	const plugin = $derived(model.details[id]);

	$effect(() => {
		void id;
		untrack(() => model.loadDetails(id));
	});

	const name = $derived(plugin?.name ?? entry?.name ?? id);
	const installed = $derived(plugin ? plugin.scope !== 'marketplace' : !!entry?.installed);
	const enabled = $derived(plugin?.enabled ?? entry?.enabled ?? false);
	const authored = $derived(plugin?.trust === 'authored' || !!entry?.authored);
	const needsReview = $derived(plugin ? plugin.approval === 'pending' : !!entry?.review);
	const update = $derived(entry?.update);
	const busy = $derived(model.busy === id);

	const commands = $derived(plugin?.commands ?? []);
	const agents = $derived(plugin?.agents ?? []);
	const skills = $derived(plugin?.skills ?? []);
	const hasContents = $derived(commands.length + agents.length + skills.length > 0);
	/** Components from the details, else from the list row (always loaded with the page). */
	const components = $derived(plugin?.components ?? entry?.info?.components ?? []);
	const runs = $derived(runsCode(components));
	const adapted = $derived(adaptedParts(components));
	const leftOut = $derived(leftOutParts(components));
	const services = $derived(declaredServices(components));
	const otherParts = $derived(components.filter((c) => c.kind !== 'service'));
	const readme = $derived(plugin?.readme ? sanitizeHtml(renderReadme(plugin.readme)) : '');

	const tabs = $derived([...(needsReview ? ['Review'] : []), 'Overview', ...(hasContents ? ['Contents'] : [])]);

	/** The "before you turn it on" card is open. */
	let reviewing = $state(false);
	let reviewCard: HTMLElement | undefined = $state();

	async function openReview() {
		reviewing = true;
		await tick();
		reviewCard?.focus();
	}

	/** Switch handler: turning on a plugin that runs code asks first. */
	function onSwitch(on: boolean): Promise<boolean> {
		if (on && runs.length) {
			void openReview();
			return Promise.resolve(false);
		}
		reviewing = false;
		return model.setEnabled(id, on);
	}

	async function confirmEnable() {
		if (await model.setEnabled(id, true)) reviewing = false;
	}

	function reviewKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			event.stopPropagation();
			reviewing = false;
		}
	}

	function formatDate(value: string | null | undefined): string | null {
		if (!value) return null;
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return null;
		return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
	}

	/** `sha256:9f2ac71e…a9b8c7`: enough to compare, short enough to fit. */
	function shortHash(hash: string | null | undefined): string {
		if (!hash) return 'Missing';
		return hash.length > 28 ? `${hash.slice(0, 19)}…${hash.slice(-6)}` : hash;
	}

	const version = $derived(plugin?.version ?? entry?.version);
	const facts = $derived<Fact[]>([
		{ label: 'Version', value: version ? `v${version}` : null, mono: true },
		{ label: 'Source', value: plugin?.marketplace ?? entry?.marketplace },
		{ label: 'Installed', value: installed ? formatDate(plugin?.installed_at) : null },
		{ label: 'Scope', value: installed ? plugin?.scope : null }
	]);

	const status = $derived(needsReview ? 'warn' : installed ? (enabled ? 'ok' : 'off') : 'standby');
	const statusText = $derived(
		needsReview ? NEEDS_REVIEW : installed ? (enabled ? 'On' : 'Off') : 'Not installed'
	);
	const badge = $derived(
		authored ? 'Agent draft' : needsReview ? 'Agent install' : (FORMAT_LABELS[plugin?.format ?? ''] ?? 'Plugin')
	);

	const danger = $derived<DangerAction[]>(
		installed
			? [
					{
						label: authored ? 'Discard draft' : 'Uninstall',
						hint: 'Removes its skills and hooks',
						button: authored ? 'Discard' : 'Uninstall',
						question: `Uninstall ${name}?`,
						onConfirm: async () => {
							await model.uninstall(id);
						},
						disabled: busy
					}
				]
			: []
	);
</script>

{#snippet primary()}
	{#if !installed}
		<button type="button" class="st-btn primary" disabled={busy} onclick={() => model.install(id)}>
			<Download />{busy ? 'Installing…' : 'Install'}
		</button>
	{:else if !needsReview}
		<Switch checked={enabled} label="Enabled" disabled={busy || reviewing} onchange={onSwitch} />
	{/if}
{/snippet}

{#snippet banner()}
	{#if model.actionError}
		<InlineBanner tone="warn"><span role="alert">{model.actionError}</span></InlineBanner>
	{/if}
	{#if model.detailsError[id] && !plugin}
		<InlineBanner tone="warn">
			<span role="alert">{model.detailsError[id]}</span>
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => model.loadDetails(id)}>Retry</button>
			{/snippet}
		</InlineBanner>
	{/if}
	{#if reviewing && !needsReview}
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<section
			class="enable-review st-card"
			aria-labelledby="plugin-enable-review-title"
			tabindex="-1"
			bind:this={reviewCard}
			onkeydown={reviewKeydown}
			data-testid="plugin-enable-review"
		>
			<header>
				<span class="ic" aria-hidden="true"><ShieldAlert /></span>
				<span>
					<h3 id="plugin-enable-review-title">Before you turn it on</h3>
					<p>It runs code on this device.</p>
				</span>
			</header>
			<RunsCodeList items={runs} />
			<footer>
				<button type="button" class="st-btn ghost sm" disabled={busy} onclick={() => (reviewing = false)}>Cancel</button>
				<button type="button" class="st-btn primary sm" disabled={busy} onclick={confirmEnable}>
					{busy ? 'Turning on…' : 'Turn on'}
				</button>
			</footer>
		</section>
	{/if}
	{#if needsReview}
		<InlineBanner tone="warn">
			{authored ? 'Written by an agent. Review first.' : 'Added by an agent. Review first.'}
		</InlineBanner>
	{:else if update && installed}
		<InlineBanner tone="info" icon={ArrowUpCircle}>
			Update to v{update.available_version}
			{#snippet actions()}
				<button type="button" class="st-btn sm" disabled={!!model.updating[id]} onclick={() => model.update(id)}>
					{model.updating[id] ? 'Updating…' : 'Update'}
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

{#snippet servicesGroup()}
	{#if services.length}
		<div data-testid="plugin-services">
			<RowGroup title="Uses outside services" count={services.length}>
				{#each services as service (service)}
					<SettingRow label={service} hint="Asks you for access" />
				{/each}
			</RowGroup>
		</div>
	{/if}
{/snippet}

<DetailPane
	{name}
	icon={needsReview ? REVIEW_ICON : PACKAGE_ICON}
	{status}
	{statusText}
	{badge}
	subtitle={plugin?.description || entry?.description || undefined}
	{primary}
	{facts}
	{banner}
	{tabs}
	category="plugins"
	{item}
	{danger}
>
	{#snippet children(tab)}
		{#if !plugin}
			<p class="st-empty" role="status">{model.detailsError[id] ? '' : 'Loading details…'}</p>
		{:else if tab === 'Review'}
			<div data-testid="plugin-review">
				<RowGroup title="This version">
					<SettingRow label="Version"><ValueText value={plugin.version} mono /></SettingRow>
					{#if authored}
						<SettingRow label="Content hash"><ValueText value={shortHash(plugin.content_hash)} mono /></SettingRow>
					{/if}
					<SettingRow label="Files" hint={plugin.install_path} hintMono />
				</RowGroup>
				{#if runs.length}
					<RowGroup title="Runs code" count={runs.length}>
						<RunsCodeList items={runs} />
					</RowGroup>
				{/if}
				<RowGroup title="What it adds" count={otherParts.length}>
					{#each otherParts as component (component.kind + ':' + component.name)}
						<SettingRow label="{componentLabel(component.kind)} {component.name}" />
					{:else}
						<SettingRow label="Nothing listed" />
					{/each}
				</RowGroup>
				{@render servicesGroup()}
				<RowGroup title="Approval">
					<SettingRow label="Approve and enable" hint={authored ? 'Pins this exact version' : undefined}>
						<InlineConfirm
							label="Approve and enable"
							question="Approve v{plugin.version}?"
							confirmLabel="Approve"
							tone="primary"
							variant="primary"
							disabled={model.approving || (authored && !plugin.content_hash)}
							onConfirm={async () => {
								await model.approve(id);
							}}
						/>
					</SettingRow>
				</RowGroup>
			</div>
		{:else if tab === 'Contents'}
			{#if commands.length}
				<RowGroup title="Commands" count={commands.length}>
					{#each commands as command (command)}
						<SettingRow label="/{command}" />
					{/each}
				</RowGroup>
			{/if}
			{#if agents.length}
				<RowGroup title="Agents" count={agents.length}>
					{#each agents as agent (agent)}
						<SettingRow label={agent} />
					{/each}
				</RowGroup>
			{/if}
			{#if skills.length}
				<RowGroup title="Skills" count={skills.length}>
					{#each skills as skill (skill)}
						<SettingRow label={skill} />
					{/each}
				</RowGroup>
			{/if}
		{:else}
			{#if hasContents || plugin.has_hooks}
				<RowGroup title="Includes">
					{#if commands.length}<SettingRow label="Commands"><ValueText value={commands.length} /></SettingRow>{/if}
					{#if agents.length}<SettingRow label="Agents"><ValueText value={agents.length} /></SettingRow>{/if}
					{#if skills.length}<SettingRow label="Skills"><ValueText value={skills.length} /></SettingRow>{/if}
					{#if plugin.has_hooks}<SettingRow label="Hooks"><ValueText value="Yes" /></SettingRow>{/if}
				</RowGroup>
			{/if}
			{#if installed && services.length}
				<PluginConnections pluginId={id} {services} {enabled} />
			{:else}
				{@render servicesGroup()}
			{/if}
			{#if adapted.length}
				<div data-testid="plugin-adapted">
					<RowGroup title="Adapted for AI Shuffle" count={adapted.length}>
						<PartNotes notes={adapted} />
					</RowGroup>
				</div>
			{/if}
			{#if leftOut.length}
				<div data-testid="plugin-not-loaded">
					<RowGroup title="Not loaded here" count={leftOut.length}>
						<PartNotes notes={leftOut} />
					</RowGroup>
				</div>
			{/if}
			<RowGroup title="README" bare>
				{#if readme}
					<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized above -->
					<div class="readme st-card">{@html readme}</div>
				{:else}
					<p class="st-empty none">No README</p>
				{/if}
			</RowGroup>
			{#if installed}
				<RowGroup title="Location">
					<SettingRow label="Install path" hint={plugin.install_path} hintMono />
				</RowGroup>
			{/if}
		{/if}
	{/snippet}
</DetailPane>

<style>
	.enable-review {
		margin-top: 12px;
		border-color: var(--st-warning-line);
		outline: none;
		overflow: hidden;
	}
	.enable-review:focus-visible {
		box-shadow: 0 0 0 2px var(--st-primary-line);
	}
	.enable-review header {
		display: flex;
		gap: 10px;
		align-items: flex-start;
		padding: 12px 14px 10px;
		border-bottom: 1px solid var(--st-border);
	}
	.enable-review header .ic {
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		flex: none;
		border-radius: var(--st-radius-sm);
		background: var(--st-warning-soft);
		color: var(--st-warning);
	}
	.enable-review header .ic :global(svg) {
		width: 15px;
		height: 15px;
	}
	.enable-review h3 {
		margin: 0;
		font-size: 14px;
		font-weight: 600;
	}
	.enable-review p {
		margin: 2px 0 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.enable-review footer {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		gap: 8px;
		padding: 10px 14px;
		border-top: 1px solid var(--st-border);
		background: var(--st-muted);
	}
	.readme {
		padding: 4px 16px;
		font-size: 13.5px;
		line-height: 1.55;
		overflow-wrap: anywhere;
	}
	.readme :global(:is(h3, h4, h5)) {
		font-size: 14px;
		font-weight: 600;
		margin: 14px 0 6px;
	}
	.readme :global(p),
	.readme :global(ul),
	.readme :global(ol) {
		margin: 8px 0;
	}
	.readme :global(:is(ul, ol)) {
		padding-left: 20px;
	}
	.readme :global(ul) {
		list-style: disc;
	}
	.readme :global(ol) {
		list-style: decimal;
	}
	.readme :global(code) {
		font-family: var(--st-mono);
		font-size: 12px;
		background: var(--st-muted);
		border-radius: var(--st-radius-sm);
		padding: 1px 4px;
	}
	.readme :global(pre) {
		background: var(--st-code-bg);
		border-radius: var(--st-radius-md);
		padding: 10px 12px;
		overflow-x: auto;
	}
	.readme :global(pre code) {
		background: none;
		padding: 0;
	}
	.readme :global(a) {
		color: var(--st-primary);
	}
	.readme :global(hr) {
		border: 0;
		border-top: 1px solid var(--st-border);
	}
	.none {
		padding: 4px 2px;
	}
</style>
