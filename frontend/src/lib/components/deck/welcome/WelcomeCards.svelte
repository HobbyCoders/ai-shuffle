<script lang="ts">
	import { MessageSquare, Bot, Terminal } from 'lucide-svelte';
	import WelcomeCard from './WelcomeCard.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
	}

	let { onCreateCard }: Props = $props();

	const cards = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
			suit: '♠' as const,
			shortcut: '⌘N',
			rotation: -5
		},
		{
			type: 'agent',
			label: 'AGENT',
			description: 'Deploy an AI ally',
			icon: Bot,
			suit: '♥' as const,
			shortcut: '⌘⇧B',
			rotation: 0
		},
		{
			type: 'terminal',
			label: 'TERMINAL',
			description: 'Open command line',
			icon: Terminal,
			suit: '♦' as const,
			shortcut: '⌘T',
			rotation: 5
		}
	];
</script>

<div class="cards-container">
	{#each cards as card, i}
		<WelcomeCard {...card} index={i} onclick={() => onCreateCard(card.type)} />
	{/each}
</div>

<style>
	.cards-container {
		display: flex;
		gap: 1.75rem;
		perspective: 1200px;
		margin-top: 0.5rem;
	}

	@media (max-width: 640px) {
		.cards-container {
			flex-direction: column;
			gap: 1rem;
			align-items: center;
		}
	}
</style>
