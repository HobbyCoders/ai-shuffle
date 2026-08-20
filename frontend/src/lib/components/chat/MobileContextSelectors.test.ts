import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';
import ProfileSelector from './ProfileSelector.svelte';
import BranchSelector from './BranchSelector.svelte';

beforeEach(() => openHeaderDropdown.set(null));

afterEach(() => {
	cleanup();
	openHeaderDropdown.set(null);
});

describe('mobile sheet context selectors', () => {
	it('opens only the sheet agent menu when the hidden composer copy is mounted', async () => {
		render(ProfileSelector);
		const sheet = render(ProfileSelector, { props: { sheetMode: true } });

		await fireEvent.click(within(sheet.container).getByRole('button', { name: 'Agent' }));

		expect(within(sheet.container).getByRole('menu', { name: 'Agent selection' })).toBeTruthy();
		expect(screen.getAllByRole('menu', { name: 'Agent selection' })).toHaveLength(1);
	});

	it('opens only the sheet branch menu and closes the sheet agent menu', async () => {
		render(ProfileSelector);
		render(BranchSelector);
		const sheetProfile = render(ProfileSelector, { props: { sheetMode: true } });
		const sheetBranch = render(BranchSelector, { props: { sheetMode: true } });

		await fireEvent.click(within(sheetProfile.container).getByRole('button', { name: 'Agent' }));
		expect(within(sheetProfile.container).getByRole('menu', { name: 'Agent selection' })).toBeTruthy();

		const branchButton = sheetBranch.container.querySelector<HTMLButtonElement>('button.branch-btn');
		expect(branchButton).toBeTruthy();
		await fireEvent.click(branchButton!);

		expect(within(sheetBranch.container).getByRole('menu', { name: 'Branch selection' })).toBeTruthy();
		expect(screen.getAllByRole('menu', { name: 'Branch selection' })).toHaveLength(1);
		expect(screen.queryByRole('menu', { name: 'Agent selection' })).toBeNull();
	});
});
