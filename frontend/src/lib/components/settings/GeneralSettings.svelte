<script lang="ts">
	/**
	 * GeneralSettings - Theme, workspace path, and timezone settings
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { theme, themePreference, currentPresetId, themePresets } from '$lib/stores/theme.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig } from '$lib/api/auth';
	import { getErrorMessage } from '$lib/api/client';
	import type { WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import { onMount } from 'svelte';

	interface Props {
		isAdmin: boolean;
	}

	let { isAdmin }: Props = $props();

	let expanded = $state(true);

	// Workspace state
	let workspacePath = $state('');
	let workspaceValidation: WorkspaceValidation | null = $state(null);
	let validatingWorkspace = $state(false);
	let savingWorkspace = $state(false);
	let workspaceError = $state('');
	let workspaceSuccess = $state('');

	// Timezone state
	let currentTimezone = $state('local');

	onMount(() => {
		if (isAdmin) {
			loadWorkspaceConfig();
		}
		timezonePreference.load();
		const unsubTz = timezonePreference.subscribe((tz: string) => {
			currentTimezone = tz;
		});
		return () => { unsubTz(); };
	});

	async function loadWorkspaceConfig() {
		try {
			const config = await getWorkspaceConfig();
			workspacePath = config.workspace_path;
		} catch (e) {
			console.error('Failed to load workspace config:', e);
		}
	}

	async function handleValidateWorkspace() {
		if (!workspacePath.trim()) return;
		validatingWorkspace = true;
		workspaceError = '';
		try {
			workspaceValidation = await validateWorkspacePath(workspacePath);
		} catch (e: unknown) {
			workspaceError = getErrorMessage(e, 'Validation failed');
		} finally {
			validatingWorkspace = false;
		}
	}

	async function handleSaveWorkspace() {
		if (!workspacePath.trim()) return;
		savingWorkspace = true;
		workspaceError = '';
		workspaceSuccess = '';
		try {
			await setWorkspaceConfig(workspacePath);
			workspaceSuccess = 'Workspace saved';
			setTimeout(() => workspaceSuccess = '', 3000);
		} catch (e: unknown) {
			workspaceError = getErrorMessage(e, 'Failed to save');
		} finally {
			savingWorkspace = false;
		}
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>General</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- Theme -->
			<div class="setting-row">
				<div class="setting-info">
					<span>Theme</span>
					<span class="setting-desc">Choose your preferred appearance</span>
				</div>
				<div class="theme-switcher">
					<button
						class="theme-option"
						class:active={$themePreference === 'light'}
						onclick={() => theme.setTheme('light')}
						aria-label="Light theme"
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<circle cx="12" cy="12" r="5"/>
							<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
						</svg>
					</button>
					<button
						class="theme-option"
						class:active={$themePreference === 'dark'}
						onclick={() => theme.setTheme('dark')}
						aria-label="Dark theme"
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
						</svg>
					</button>
					<button
						class="theme-option"
						class:active={$themePreference === 'system'}
						onclick={() => theme.setTheme('system')}
						aria-label="System theme"
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
							<line x1="8" y1="21" x2="16" y2="21"/>
							<line x1="12" y1="17" x2="12" y2="21"/>
						</svg>
					</button>
				</div>
			</div>

			<!-- Theme Preset -->
			<div class="setting-row">
				<div class="setting-info">
					<label for="theme-preset">Theme Style</label>
					<span class="setting-desc">Choose a color palette</span>
				</div>
				<select
					id="theme-preset"
					class="input"
					value={$currentPresetId ?? ''}
					onchange={(e) => theme.setPreset(e.currentTarget.value || null)}
				>
					<option value="">Default</option>
					{#each themePresets as preset (preset.id)}
						<option value={preset.id}>{preset.label}</option>
					{/each}
				</select>
			</div>

			<!-- Workspace Path -->
			{#if isAdmin}
				<div class="setting-row vertical">
					<div class="setting-info">
						<label for="general-workspace-path">Workspace Path</label>
						<span class="setting-desc">Base directory for projects</span>
					</div>
					<div class="input-group">
						<input
							id="general-workspace-path"
							type="text"
							bind:value={workspacePath}
							placeholder="/path/to/workspace"
							class="input"
						/>
						<button
							onclick={handleValidateWorkspace}
							disabled={validatingWorkspace || !workspacePath.trim()}
							class="btn secondary"
						>
							{validatingWorkspace ? '...' : 'Validate'}
						</button>
						<button
							onclick={handleSaveWorkspace}
							disabled={savingWorkspace || !workspacePath.trim()}
							class="btn primary"
						>
							{savingWorkspace ? '...' : 'Save'}
						</button>
					</div>
					{#if workspaceValidation}
						<div class="validation-result" class:valid={workspaceValidation.valid}>
							{workspaceValidation.valid ? 'Valid path' : `${workspaceValidation.error}`}
						</div>
					{/if}
					{#if workspaceSuccess}
						<div class="success-msg">{workspaceSuccess}</div>
					{/if}
					{#if workspaceError}
						<div class="error-msg">{workspaceError}</div>
					{/if}
				</div>
			{/if}

			<!-- Timezone -->
			<div class="setting-row">
				<div class="setting-info">
					<label for="general-timezone">Timezone</label>
					<span class="setting-desc">Used for timestamps across the UI</span>
				</div>
				<select
					id="general-timezone"
					class="select"
					value={currentTimezone}
					onchange={(e) => timezonePreference.setTimezone(e.currentTarget.value)}
				>
					<option value="local">Browser Default</option>
					<option value="UTC">UTC</option>
					<optgroup label="Americas">
						<option value="America/New_York">Eastern (New York)</option>
						<option value="America/Chicago">Central (Chicago)</option>
						<option value="America/Denver">Mountain (Denver)</option>
						<option value="America/Los_Angeles">Pacific (Los Angeles)</option>
						<option value="America/Anchorage">Alaska</option>
						<option value="Pacific/Honolulu">Hawaii</option>
						<option value="America/Sao_Paulo">S&atilde;o Paulo</option>
					</optgroup>
					<optgroup label="Europe &amp; Africa">
						<option value="Europe/London">London</option>
						<option value="Europe/Paris">Paris / Berlin</option>
						<option value="Europe/Moscow">Moscow</option>
						<option value="Africa/Cairo">Cairo</option>
					</optgroup>
					<optgroup label="Asia &amp; Pacific">
						<option value="Asia/Dubai">Dubai</option>
						<option value="Asia/Kolkata">India (Kolkata)</option>
						<option value="Asia/Shanghai">China (Shanghai)</option>
						<option value="Asia/Tokyo">Tokyo</option>
						<option value="Asia/Seoul">Seoul</option>
						<option value="Australia/Sydney">Sydney</option>
						<option value="Pacific/Auckland">Auckland</option>
					</optgroup>
				</select>
			</div>
		</div>
	{/if}
</section>

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.setting-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 8px 0;
	}

	.setting-row.vertical {
		flex-direction: column;
		align-items: stretch;
	}

	.setting-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.setting-info label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.setting-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.input::placeholder { color: var(--muted-foreground); }

	.input-group {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.input-group .input { flex: 1; }

	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }

	.btn.secondary {
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
	}

	.btn.secondary:hover:not(:disabled) { background: var(--hover-overlay); }

	.theme-switcher {
		display: flex;
		gap: 4px;
		background: var(--muted);
		padding: 4px;
		border-radius: var(--radius);
	}

	.theme-option {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 32px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.theme-option:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.theme-option:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.theme-option.active {
		background: var(--card);
		color: var(--primary);
		box-shadow: var(--shadow-s);
	}

	.theme-option svg { width: 18px; height: 18px; }

	.validation-result {
		font-size: 0.8125rem;
		padding: 6px 10px;
		border-radius: calc(var(--radius) - 2px);
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		color: var(--destructive);
	}

	.validation-result.valid {
		background: color-mix(in oklch, var(--success) 10%, transparent);
		color: var(--success);
	}

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}
</style>
