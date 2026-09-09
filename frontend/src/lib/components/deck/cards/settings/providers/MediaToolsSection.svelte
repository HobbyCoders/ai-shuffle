<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	import { apiBase, withAuth } from '$lib/api/runtime';
	/**
	 * MediaToolsSection — the "Media & tools" settings group (Chunk 8, plan §1.5).
	 *
	 * Folds the two residual Connections groups (Media & tools / fal.ai and
	 * Integrations / GitHub) into a single group, and adds a "Manage media models"
	 * keycard row that opens `ManageMediaModelsDrawer` — the new home for the old
	 * AI-models tab (ruling D5). Replaces the residual `ConnectionsSection`.
	 *
	 * Card order: fal.ai API key, GitHub, AI media models — the credentials
	 * first, then the model catalog the fal key unlocks.
	 *
	 * The fal.ai keycard mechanics are lifted verbatim from ConnectionsSection
	 * (self-contained local state + raw fetch to /settings/integrations/fal-ai).
	 * The GitHub keycard is driven by the same six props ProvidersSection forwards.
	 *
	 * Rendered inside `.ed-content` like ProvidersSection's other groups, so the
	 * shell `.set-group` / `.ed-note` frame classes still apply; the keycard CSS is
	 * copied here so the component is self-contained.
	 */
	import { onMount } from 'svelte';
	import { Image } from 'lucide-svelte';
	import ManageMediaModelsDrawer from './ManageMediaModelsDrawer.svelte';

	interface Props {
		githubUser: string | null;
		githubToken: string;
		onGithubTokenChange: (value: string) => void;
		githubLoginLoading: boolean;
		onConnectGithub: () => void;
		onDisconnectGithub: () => void;
	}

	let {
		githubUser,
		githubToken,
		onGithubTokenChange,
		githubLoginLoading,
		onConnectGithub,
		onDisconnectGithub,
	}: Props = $props();

	// ----- fal.ai key (self-contained, lifted verbatim from ConnectionsSection) -----
	let falKeySet = $state(false);
	let falKeyMasked = $state('');
	let falKeyInput = $state('');
	let falSaving = $state(false);
	let falError = $state('');
	let falSuccess = $state('');

	// The fal key comes from the /settings/integrations payload.
	async function loadKeyStatus() {
		try {
			const res = await fetch(`${apiBase()}/settings/integrations`, withAuth({ }));
			if (!res.ok) return;
			const data = await res.json();
			falKeySet = !!data.fal_api_key_set;
			falKeyMasked = data.fal_api_key_masked ?? '';
		} catch (e) {
			// Silent — non-critical, user can retry by reopening settings
			console.warn('Failed to load credential status', e);
		}
	}

	async function saveFalKey() {
		if (!falKeyInput.trim()) return;
		falSaving = true;
		falError = '';
		falSuccess = '';
		try {
			const res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: falKeyInput.trim() }),
			}));
			if (!res.ok) {
				const body = await res.json().catch(() => ({ }));
				falError = body.detail ?? `Save failed (${res.status})`;
				return;
			}
			const data = await res.json();
			falKeySet = true;
			falKeyMasked = data.masked_key ?? '';
			falKeyInput = '';
			falSuccess = data.valid ? 'Saved & verified' : 'Saved (could not verify)';
			setTimeout(() => (falSuccess = ''), 2500);
		} catch (e) {
			falError = e instanceof Error ? e.message : 'Network error';
		} finally {
			falSaving = false;
		}
	}

	async function removeFalKey() {
		falSaving = true;
		falError = '';
		falSuccess = '';
		try {
			const res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({
				method: 'DELETE',
			}));
			if (!res.ok) {
				falError = `Remove failed (${res.status})`;
				return;
			}
			falKeySet = false;
			falKeyMasked = '';
			falSuccess = 'Removed';
			setTimeout(() => (falSuccess = ''), 2500);
		} catch (e) {
			falError = e instanceof Error ? e.message : 'Network error';
		} finally {
			falSaving = false;
		}
	}

	// Fetch on mount. No reactivity needed — key status only changes via this
	// component's own save/remove actions.
	onMount(() => {
		loadKeyStatus();
	});

	// ----- Manage media models drawer (fold of the old AI-models tab, ruling D5) -----
	let manageMediaOpen = $state(false);
	watchResources(['settings'], () => loadKeyStatus());
</script>

<!-- Media & tools -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Image />Media &amp; tools</h3>
		<p>Keys and models for the image, video, audio and 3D generation tools.</p>
	</div>

	<!-- fal.ai key -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
				<rect x="3" y="3" width="18" height="18" rx="2"/>
				<circle cx="9" cy="9" r="2"/>
				<path d="m21 15-3.6-3.6a2 2 0 0 0-2.8 0L6 20"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>fal.ai API key
				{#if falKeySet}<span class="sb ok">Set</span>{:else}<span class="sb off">Optional</span>{/if}
			</h4>
			{#if falKeySet}
				<p>One key powers every AI media tool. <code class="kc-val">{falKeyMasked}</code></p>
			{:else}
				<p>Image, video, audio &amp; 3D generation &amp; editing tools.</p>
			{/if}
		</div>
		<div class="kc-acts">
			{#if falKeySet}
				<button type="button" class="ed-btn danger" onclick={removeFalKey} disabled={falSaving}>Remove</button>
			{:else}
				<input
					type="password"
					autocomplete="off"
					value={falKeyInput}
					oninput={(e) => (falKeyInput = e.currentTarget.value)}
					placeholder="Enter your fal.ai API key"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={saveFalKey} disabled={falSaving || !falKeyInput.trim()}>
					{falSaving ? '…' : 'Save'}
				</button>
			{/if}
		</div>
	</div>

	{#if falError}<div class="kc-foot err" role="alert">{falError}</div>{/if}
	{#if falSuccess}<div class="kc-foot ok">{falSuccess}</div>{/if}

	<!-- GitHub (verbatim; driven by the six forwarded props) -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="currentColor">
				<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>GitHub
				{#if githubUser}<span class="sb ok">@{githubUser}</span>{:else}<span class="sb off">Not connected</span>{/if}
			</h4>
			<p>Clone, branch and push project repositories.</p>
		</div>
		<div class="kc-acts">
			{#if githubUser}
				<button type="button" class="ed-btn danger" onclick={onDisconnectGithub}>Disconnect</button>
			{:else}
				<input
					type="password"
					autocomplete="off"
					value={githubToken}
					oninput={(e) => onGithubTokenChange(e.currentTarget.value)}
					placeholder="Personal access token"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={onConnectGithub} disabled={githubLoginLoading || !githubToken}>
					{githubLoginLoading ? '…' : 'Connect'}
				</button>
			{/if}
		</div>
	</div>
	<!-- AI media models (opens the manage-media-models drawer) -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<rect x="3" y="3" width="7" height="7" rx="1"/>
				<rect x="14" y="3" width="7" height="7" rx="1"/>
				<rect x="3" y="14" width="7" height="7" rx="1"/>
				<rect x="14" y="14" width="7" height="7" rx="1"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>AI media models</h4>
			<p>Image, video, audio and 3D generation models</p>
		</div>
		<div class="kc-acts">
			<button type="button" class="ed-btn" onclick={() => (manageMediaOpen = true)}>Manage</button>
		</div>
	</div>

</div>

{#if manageMediaOpen}
	<ManageMediaModelsDrawer onClose={() => (manageMediaOpen = false)} />
{/if}

<style>
	.keycard {
		display: flex;
		gap: 14px;
		align-items: flex-start;
		padding: 14px 18px;
		border-bottom: 1px solid var(--border);
	}
	.keycard:last-child {
		border-bottom: none;
	}
	.kc-ic {
		flex-shrink: 0;
		width: 38px;
		height: 38px;
		border-radius: 10px;
		display: grid;
		place-items: center;
		background: var(--muted);
		color: var(--foreground);
	}
	.kc-ic :global(svg) {
		width: 20px;
		height: 20px;
	}
	.kc-body {
		flex: 1;
		min-width: 0;
	}
	.kc-body h4 {
		margin: 1px 0 3px;
		font-size: 13.5px;
		font-weight: 600;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.kc-body p {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.kc-val {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	.kc-acts {
		flex-shrink: 0;
		display: flex;
		gap: 8px;
		align-items: center;
		/* Vertically center the action column against the taller left body
		   (the card itself stays flex-start so the icon aligns with the title). */
		align-self: center;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	/* Every action button reads the same width so the column lines up down the
	   group, whatever the label says. */
	.kc-acts .ed-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 104px;
		text-align: center;
	}
	.kc-acts .kc-in {
		width: 200px;
		min-width: 0;
	}
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.kc-foot {
		padding: 8px 18px 12px;
		font-size: 12.5px;
	}
	.kc-foot.ok {
		color: var(--success, #16a34a);
	}
	.kc-foot.err {
		color: var(--destructive);
	}

	@media (max-width: 560px) {
		/* Let the card wrap so the action area drops onto its own full-width
		   row. Without this the flex-shrink:0 actions column demands 100% width
		   in the same row and crushes the body text to one word per line. */
		.keycard {
			flex-wrap: wrap;
		}
		.kc-acts {
			width: 100%;
			justify-content: flex-start;
		}
		.kc-acts .kc-in {
			width: 100%;
		}
	}
</style>
