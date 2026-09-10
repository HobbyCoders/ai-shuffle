<script lang="ts">
	import { modelEffortOptions, resolveModelEffort, reconcileModelEffort } from '$lib/constants/model-effort';
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ProvidersSection — the "Providers" settings tab (Chunk 4 build-out).
	 *
	 * Replaces the Chunk-3 passthrough shell with the real, store-driven provider
	 * surface. It owns Group A of the plan (§1.1): the intro callout and the
	 * unified `ProviderList` (Claude subscription, Claude API key, then the
	 * model_providers rows).
	 *
	 * The remaining credential groups (Media & tools / fal, Integrations / GitHub)
	 * now live in the self-contained `MediaToolsSection` (lifted out of ConnectionsSection, which
	 * is now removed, and folded the old AI-models tab into a "Manage media models"
	 * drawer there).
	 *
	 * Data is refetched on every mount via `providers.refreshAll()`: the settings
	 * sections remount on every tab switch, so re-opening the Providers tab always
	 * reconciles with the server (this fixes a Codex sign-in that completes after
	 * the UI stopped watching leaving the row stuck on "Not connected"). The refresh
	 * is SILENT — loaders keep their loading flags down when data already renders,
	 * so nothing flickers. (`setup/+page.svelte` still uses the one-shot
	 * `ensureLoaded()`; that path is untouched.)
	 */
	import { onMount } from 'svelte';
	import { Server, FileInput, Type } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import MediaToolsSection from './MediaToolsSection.svelte';
	import ProviderList from './ProviderList.svelte';
	import SettingsImportDialog from './SettingsImportDialog.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import {
		loadTextGenerationSettings,
		saveTextGenerationSettings
	} from '$lib/api/text-generation';
	import { getDefaultModel } from '$lib/constants/models';
	import { notify } from '$lib/stores/notifications';



	let importDialogOpen = $state(false);
	let importOpener = $state<HTMLButtonElement | null>(null);

	function onImportSettings(event: MouseEvent) {
		importOpener = event.currentTarget as HTMLButtonElement;
		importDialogOpen = true;
	}

	function closeImportDialog() {
		importDialogOpen = false;
		queueMicrotask(() => importOpener?.focus());
	}

	// ── Text generation ────────────────────────────────────────────────
	// One model + effort pair behind every internal one-shot call (session
	// titles, commit messages, PR descriptions, branch names, agent
	// generation). It lives here rather than on an agent because generation
	// never runs inside a session.
	let textGenModel = $state('');
	let textGenEffort = $state('');
	let textGenSaving = $state(false);

	const EFFORT_OPTIONS = $derived([
		{ value: '', label: 'Provider default', description: 'Use the default effort' },
		...modelEffortOptions(textGenModel, textGenEffort)
	]);
	// An unset setting resolves to the registry default at call time; show that
	// same model so the row never reads as "nothing selected".
	const textGenValue = $derived(textGenModel || getDefaultModel());

	async function saveTextGeneration(patch: {
		text_generation_model?: string;
		text_generation_effort?: string;
	}) {
		const previous = { model: textGenModel, effort: textGenEffort };
		if (patch.text_generation_model !== undefined) textGenModel = patch.text_generation_model;
		if (patch.text_generation_effort !== undefined) textGenEffort = patch.text_generation_effort;
		textGenSaving = true;
		try {
			const settled = await saveTextGenerationSettings(patch);
			textGenModel = settled.text_generation_model;
			textGenEffort = settled.text_generation_effort;
		} catch (e) {
			// Roll the optimistic write back so the controls never claim a
			// binding the server refused (e.g. a model the provider dropped).
			textGenModel = previous.model;
			textGenEffort = previous.effort;
			notify(
				e instanceof Error ? e.message : 'Could not save the text generation model',
				{ severity: 'error' }
			);
		} finally {
			textGenSaving = false;
		}
	}

	onMount(() => {
		// Refetch on every card-open / tab-switch. A Codex sign-in that completes
		// after the UI stops watching would otherwise leave the row stuck on "Not
		// connected"; refetching on open reconciles it. Silent — already-rendered
		// rows don't flicker while the background refresh runs.
		providers.refreshAll();
		loadTextGenerationSettings()
			.then((settings) => {
				textGenModel = settings.text_generation_model;
				textGenEffort = settings.text_generation_effort;
			})
			.catch(() => {
				// Leave the row on its defaults — a failed read must not block the
				// rest of the providers surface.
			});
	});
	watchResources(['settings'], async () => {
		const before = JSON.stringify([textGenModel, textGenEffort]);
		const settings = await loadTextGenerationSettings();
		if (!textGenSaving && before === JSON.stringify([textGenModel, textGenEffort])) {
			textGenModel = settings.text_generation_model;
			textGenEffort = settings.text_generation_effort;
		}
	});
</script>

<!-- Group A: model providers -->
<div class="set-group" id="providers-section">
	<div class="set-grouphead">
		<h3><Server />Providers</h3>
		<button type="button" class="ed-btn prov-import-btn" onclick={onImportSettings}>
			<FileInput />Import settings
		</button>
	</div>

	<div class="prov-groupbody">
		<ProviderList />
	</div>
</div>

<!-- Group B: the one binding behind every internal one-shot call -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Type />Generated text</h3>
	</div>
	<div class="set-row">
		<div class="lab">
			<h4>Text generation model</h4>
		</div>
		<div class="ctl textgen-ctl">
			<div class="textgen-model">
				<ModelPicker
					id="text-generation-model"
					value={textGenValue}
					allowCustom={false}
					onSelect={(value) => saveTextGeneration({ text_generation_model: value,
						text_generation_effort: reconcileModelEffort(textGenEffort || null, resolveModelEffort(value)) ?? '' })}
				/>
			</div>
			<select
				class="sel textgen-effort"
				aria-label="Text generation effort"
				disabled={textGenSaving}
				value={textGenEffort}
				onchange={(e) => saveTextGeneration({ text_generation_effort: e.currentTarget.value })}
			>
				{#each EFFORT_OPTIONS as option (option.value)}
					<option value={option.value}>{option.label}</option>
				{/each}
			</select>
		</div>
	</div>
</div>

<!-- Media & tools (fal.ai, GitHub, AI media models) — served by MediaToolsSection. -->
<MediaToolsSection />

{#if importDialogOpen}
	<SettingsImportDialog onClose={closeImportDialog} />
{/if}

<style>
	/* Model picker + effort stay on one line beside the label, each at a fixed
	   width so neither squeezes the other. They stack only on a narrow card. */
	.textgen-ctl {
		display: flex;
		flex-wrap: nowrap;
		align-items: center;
		justify-content: flex-end;
		gap: 8px;
	}
	.textgen-model {
		flex-shrink: 0;
		width: 260px;
	}
	.textgen-effort {
		flex-shrink: 0;
		width: 140px;
		min-width: 0;
	}
	.prov-groupbody {
		padding: 10px 14px;
	}
	.set-grouphead {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
	}
	.prov-import-btn {
		display: inline-flex;
		min-height: 36px;
		align-items: center;
		gap: 6px;
	}
	.prov-import-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.prov-import-btn {
		color: var(--foreground);
		border-color: color-mix(in oklch, var(--primary) 28%, var(--border));
		background: color-mix(in oklch, var(--primary) 6%, transparent);
	}
	.prov-import-btn:hover {
		background: color-mix(in oklch, var(--primary) 11%, transparent);
	}
	@media (max-width: 640px) {
		.textgen-ctl {
			flex-wrap: wrap;
			width: 100%;
		}
		.textgen-model,
		.textgen-effort {
			width: 100%;
		}
	}
</style>
