<script lang="ts">
	import { Check } from 'lucide-svelte';
	let { steps, current, completed }: {
		steps: { id: string; label: string }[];
		current: string;
		completed: Set<string>;
	} = $props();
</script>

<nav aria-label="Setup progress">
	<ol style:grid-template-columns={`repeat(${steps.length}, minmax(0, 1fr))`}>
		{#each steps as step, i (step.id)}
			<li aria-current={current === step.id ? 'step' : undefined} class:complete={completed.has(step.id)}>
				<span class="number" aria-hidden="true">
					{#if completed.has(step.id)}<Check size={14} strokeWidth={2.5} />{:else}{i + 1}{/if}
				</span>
				<span class="label">{step.label}</span>
				{#if completed.has(step.id)}<span class="sr-only">Completed</span>{/if}
			</li>
		{/each}
	</ol>
</nav>

<style>
	nav { margin-bottom: 1.5rem; width: 100%; }
	ol { display: grid; gap: .25rem; padding: 0; margin: 0; list-style: none; }
	li { min-width: 0; display: flex; flex-direction: column; align-items: center; gap: .4rem; color: var(--muted-foreground); text-align: center; }
	.number { display: flex; align-items: center; justify-content: center; width: 1.75rem; height: 1.75rem; flex: none; border-radius: 50%; background: var(--muted); font-size: .75rem; font-weight: 600; }
	.label { max-width: 100%; overflow-wrap: anywhere; font-size: .75rem; line-height: 1.35; }
	li[aria-current] { color: var(--foreground); font-weight: 600; }
	li[aria-current] .number { background: var(--primary); color: var(--primary-foreground); }
	li.complete .number { background: var(--success); color: var(--success-foreground); }
</style>
