<script lang="ts">
	/**
	 * Wizard step 1: what AI Shuffle is, and the disclosures a buyer sees before
	 * anything else: agents act with this worker's access, the AI account is the
	 * buyer's own, and the app checks for updates without asking (decision D22,
	 * the same pattern as other desktop apps), with a link to what is sent.
	 */
	import { ExternalLink, RefreshCw, ShieldCheck, Wallet } from 'lucide-svelte';
	import { PRIVACY_UPDATES_URL } from './setup-steps';

	interface Props {
		/** The desktop app: agents run as the user on this computer. */
		desktop?: boolean;
		onContinue: () => void;
	}
	let { desktop = false, onContinue }: Props = $props();
</script>

<div class="space-y-4" data-testid="setup-welcome">
	<p class="text-sm text-foreground leading-relaxed">
		AI Shuffle runs AI coding agents on {desktop ? 'this computer' : 'your server'}, with a workspace for your projects
		and sessions you can follow from any of your devices.
	</p>

	<section class="disclosure" aria-labelledby="welcome-access">
		<ShieldCheck size={18} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
		<div>
			<h2 id="welcome-access">Agents act with your access</h2>
			<p>
				{#if desktop}
					Agents run as your user on this computer. They can read and change files in the projects you give them, run commands, and use the accounts you connect, such as GitHub.
				{:else}
					Agents run inside this container. They can read and change files in the projects you give them, run commands, and use the accounts you connect, such as GitHub.
				{/if}
				Keep anything you do not want an agent to touch outside the workspace.
			</p>
		</div>
	</section>

	<section class="disclosure" aria-labelledby="welcome-account">
		<Wallet size={18} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
		<div>
			<h2 id="welcome-account">You bring your own AI account</h2>
			<p>
				Agents run on your own AI provider account, with a subscription or an API key. You choose the provider in the next steps.
				AI Shuffle never pays for, resells or shares model usage. Your provider bills you directly, and your prompts go from {desktop ? 'this computer' : 'this worker'} straight to your provider.
			</p>
		</div>
	</section>

	<section class="disclosure" aria-labelledby="welcome-updates">
		<RefreshCw size={18} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
		<div>
			<h2 id="welcome-updates">Update checks</h2>
			<p>
				AI Shuffle checks for updates when it starts and once a day, without asking, like other desktop apps.
				The check sends no prompts, files or usage data. Once licensing is live it also renews this worker's license.
				You can turn updates off in Settings > System > Updates.
				<a href={PRIVACY_UPDATES_URL} target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 text-primary underline">
					What is sent <ExternalLink size={12} />
				</a>
			</p>
		</div>
	</section>

	<button type="button" onclick={onContinue} class="btn btn-primary w-full py-3">Continue</button>
</div>

<style>
	.disclosure {
		display: flex;
		gap: 0.75rem;
		padding: 0.875rem 1rem;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
	}
	.disclosure h2 {
		margin: 0 0 0.25rem;
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
	}
	.disclosure p {
		margin: 0;
		font-size: 0.8125rem;
		line-height: 1.55;
		color: var(--muted-foreground);
	}
</style>
