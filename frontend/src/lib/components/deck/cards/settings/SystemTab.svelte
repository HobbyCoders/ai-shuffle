<script lang="ts">
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import RateLimitManager from '$lib/components/RateLimitManager.svelte';
	import ApiUserManager from '$lib/components/ApiUserManager.svelte';
	import './settings-shared.css';

	interface Props {
		expandedSections: Set<string>;
		onToggleSection: (section: string) => void;
		// Security Instructions
		securityInstructions: string;
		onSecurityInstructionsChange: (value: string) => void;
		defaultSecurityInstructions: string;
		isSecurityInstructionsCustomized: boolean;
		securityInstructionsChanged: boolean;
		loadingSecurityInstructions: boolean;
		savingSecurityInstructions: boolean;
		securityInstructionsSuccess: string;
		securityInstructionsError: string;
		onSaveSecurityInstructions: () => void;
		onRevertSecurityInstructions: () => void;
	}

	let {
		expandedSections,
		onToggleSection,
		securityInstructions,
		onSecurityInstructionsChange,
		defaultSecurityInstructions,
		isSecurityInstructionsCustomized,
		securityInstructionsChanged,
		loadingSecurityInstructions,
		savingSecurityInstructions,
		securityInstructionsSuccess,
		securityInstructionsError,
		onSaveSecurityInstructions,
		onRevertSecurityInstructions,
	}: Props = $props();
</script>

<div class="tab-content" transition:fade={{ duration: 150 }}>
	<!-- Rate Limits Section -->
	<section class="section">
		<button class="section-header" onclick={() => onToggleSection('rate-limits')}>
			<h3>Rate Limits</h3>
			<svg class="chevron" class:expanded={expandedSections.has('rate-limits')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<polyline points="6 9 12 15 18 9"/>
			</svg>
		</button>
		{#if expandedSections.has('rate-limits')}
			<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
				<RateLimitManager />
			</div>
		{/if}
	</section>

	<!-- API Users Section -->
	<section class="section">
		<button class="section-header" onclick={() => onToggleSection('api-users')}>
			<h3>API Users</h3>
			<svg class="chevron" class:expanded={expandedSections.has('api-users')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<polyline points="6 9 12 15 18 9"/>
			</svg>
		</button>
		{#if expandedSections.has('api-users')}
			<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
				<ApiUserManager />
			</div>
		{/if}
	</section>

	<!-- Security Instructions Section -->
	<section class="section">
		<button class="section-header" onclick={() => onToggleSection('security-instructions')}>
			<h3>System Prompt Instructions</h3>
			<svg class="chevron" class:expanded={expandedSections.has('security-instructions')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<polyline points="6 9 12 15 18 9"/>
			</svg>
		</button>
		{#if expandedSections.has('security-instructions')}
			<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
				<p class="setting-desc mb-3">
					These instructions are prepended to all system prompts sent to Claude.
					Use this to add custom instructions that apply across all profiles.
				</p>

				{#if loadingSecurityInstructions}
					<div class="loading-text">Loading...</div>
				{:else}
					<div class="security-instructions-editor">
						<textarea
							value={securityInstructions}
							oninput={(e) => onSecurityInstructionsChange(e.currentTarget.value)}
							class="input security-textarea"
							placeholder="Enter security instructions..."
							rows="12"
						></textarea>

						<div class="security-instructions-footer">
							<div class="security-status">
								{#if isSecurityInstructionsCustomized}
									<span class="badge primary">Customized</span>
								{:else}
									<span class="badge muted">Using Default</span>
								{/if}
								{#if securityInstructionsChanged}
									<span class="badge warning">Unsaved changes</span>
								{/if}
							</div>

							<div class="security-actions">
								<button
									onclick={onRevertSecurityInstructions}
									class="btn secondary compact"
									disabled={!isSecurityInstructionsCustomized || savingSecurityInstructions}
									title="Revert to default instructions"
								>
									Revert to Default
								</button>
								<button
									onclick={onSaveSecurityInstructions}
									class="btn primary compact"
									disabled={!securityInstructionsChanged || savingSecurityInstructions}
								>
									{savingSecurityInstructions ? 'Saving...' : 'Save Changes'}
								</button>
							</div>
						</div>

						{#if securityInstructionsError}
							<p class="status error">{securityInstructionsError}</p>
						{/if}
						{#if securityInstructionsSuccess}
							<p class="status success">{securityInstructionsSuccess}</p>
						{/if}
					</div>
				{/if}
			</div>
		{/if}
	</section>
</div>
