/**
 * The AI generate dialog is named by its title, takes focus into its first
 * field, closes on Escape and hands focus back to the opener.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
vi.mock('$lib/api/client', () => ({ generateProfile: vi.fn(), generateSubagent: vi.fn(), continueGeneration: vi.fn() }));
vi.mock('$lib/constants/model-picker', () => ({ buildModelPickerCatalog: () => ({
	groups: [{ key: 'strong', name: 'Provider', subtext: 'api' }],
	options: [{ value: 'prov:strong:astra', label: 'Astra', groupKey: 'strong' }]
}) }));
import { generateProfile, generateSubagent, continueGeneration } from '$lib/api/client';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import AIGenerateDialog from './AIGenerateDialog.svelte';

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('AIGenerateDialog keyboard and focus', () => {
	it('is named, focuses its first field, closes on Escape and restores focus', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const onClose = vi.fn();
		const props = { type: 'profile' as const, isOpen: true, onClose, onGenerated: vi.fn() };
		const view = render(AIGenerateDialog, { props });
		await tick();
		await Promise.resolve();

		const dialog = screen.getByRole('dialog', { name: 'Generate Agent with AI' });
		expect(document.activeElement?.tagName).toBe('TEXTAREA');
		expect(dialog.contains(document.activeElement)).toBe(true);

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);

		await view.rerender({ ...props, isOpen: false });
		await tick();
		expect(document.activeElement).toBe(opener);
	});
});

beforeEach(() => vi.resetAllMocks());

const modelPicker = () => screen.getByRole('combobox', { name: /Generation model/ }) as HTMLSelectElement;
const openDialog = (type: 'profile' | 'subagent') => render(AIGenerateDialog, {
	props: { type, isOpen: true, onClose: vi.fn(), onGenerated: vi.fn() }
});

describe('per-request generation model', () => {
	it.each(['profile', 'subagent'] as const)('sends the selected provider binding for %s', async (type) => {
		const generate = type === 'profile' ? generateProfile : generateSubagent;
		vi.mocked(generate).mockResolvedValue({ status: 'complete', result: {} });
		openDialog(type);
		await fireEvent.change(modelPicker(), { target: { value: 'prov:strong:astra' } });
		await fireEvent.input(screen.getByLabelText('Describe what you want'), { target: { value: 'A security review specialist' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
		expect(generate).toHaveBeenCalledWith('A security review specialist', 'prov:strong:astra', false, undefined);
	});

	it('leaves the Settings binding untouched when no override is chosen', async () => {
		vi.mocked(generateProfile).mockResolvedValue({ status: 'complete', result: {} });
		openDialog('profile');
		expect(modelPicker().value).toBe('');
		await fireEvent.input(screen.getByLabelText('Describe what you want'), { target: { value: 'A security review specialist' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
		expect(generateProfile).toHaveBeenCalledWith('A security review specialist', undefined, false, undefined);
	});

	it.each(['profile', 'subagent'] as const)('keeps the %s binding through clarification and locks it during requests', async (type) => {
		const generate = type === 'profile' ? generateProfile : generateSubagent;
		let resolve!: (value: Awaited<ReturnType<typeof generateProfile>>) => void;
		vi.mocked(generate).mockReturnValue(new Promise((r) => { resolve = r; }));
		vi.mocked(continueGeneration).mockResolvedValue({ status: 'complete', result: {} });
		openDialog(type);
		await fireEvent.change(modelPicker(), { target: { value: 'prov:strong:astra' } });
		await fireEvent.input(screen.getByLabelText('Describe what you want'), { target: { value: 'A security review specialist' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
		expect(modelPicker().disabled).toBe(true);
		resolve({ status: 'needs_clarification', questions: [{ id: 'scope', question: 'Which files?' }] });
		await screen.findByText('Which files?');
		expect(modelPicker().value).toBe('prov:strong:astra');
		expect(modelPicker().disabled).toBe(true);
		await fireEvent.input(screen.getByPlaceholderText('Your answer...'), { target: { value: 'Changed files' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
		expect(continueGeneration).toHaveBeenCalledWith(type, 'A security review specialist', { scope: 'Changed files' }, 'prov:strong:astra', undefined);
	});

	it('retains the selection on an error and resets it on a fresh open', async () => {
		vi.mocked(generateProfile).mockRejectedValue({ detail: 'Provider unavailable' });
		const props = { type: 'profile' as const, isOpen: true, onClose: vi.fn(), onGenerated: vi.fn() };
		const view = render(AIGenerateDialog, { props });
		await fireEvent.change(modelPicker(), { target: { value: 'prov:strong:astra' } });
		await fireEvent.input(screen.getByLabelText('Describe what you want'), { target: { value: 'A security review specialist' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
		await screen.findByText('Provider unavailable');
		expect(modelPicker().value).toBe('prov:strong:astra');
		expect(modelPicker().disabled).toBe(false);
		await view.rerender({ ...props, isOpen: false });
		await view.rerender(props);
		expect(modelPicker().value).toBe('');
	});
});
