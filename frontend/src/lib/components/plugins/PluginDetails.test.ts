/**
 * PluginDetails rendered from real GET /plugins/{id} payloads
 * (see tests/api/test_plugins_frontend_contract.py for how they are produced).
 *
 * The backend reports hooks only as `has_hooks` and sends `installed_at: null`
 * for a plugin that is not installed yet; the component must work from those
 * fields rather than a hook list or a date it never receives.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import type { PluginDetails as PluginDetailsType } from '$lib/api/plugins';
import responses from '$lib/api/__fixtures__/plugin-responses.json';
import PluginDetails from './PluginDetails.svelte';

afterEach(cleanup);

const installedDetails: PluginDetailsType = responses.details_installed;
const marketplaceDetails: PluginDetailsType = responses.details_available;

describe('PluginDetails with backend payloads', () => {
	it('shows an installed plugin: version, install date, feature badges and name lists', async () => {
		const page = render(PluginDetails, { props: { plugin: installedDetails } });

		expect(page.getByRole('heading', { name: 'toolkit' })).toBeTruthy();
		expect(page.getByText('v1.2.0')).toBeTruthy();
		expect(page.getByText('Enabled')).toBeTruthy();
		const installedOn = new Date(installedDetails.installed_at!).toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'long',
			day: 'numeric'
		});
		expect(page.getByText(`Installed ${installedOn}`)).toBeTruthy();
		expect(page.getByText('1 Commands')).toBeTruthy();
		expect(page.getByText('1 Agents')).toBeTruthy();
		expect(page.getByText('1 Skills')).toBeTruthy();
		// has_hooks drives the badge; there is no hook list, so no count and no tab.
		expect(page.getByText('Hooks')).toBeTruthy();
		expect(page.queryByRole('button', { name: 'Hooks' })).toBeNull();
		expect(page.getByText('Review helpers for pull requests.')).toBeTruthy();

		await fireEvent.click(page.getByRole('button', { name: 'Commands' }));
		expect(page.getByText('/review')).toBeTruthy();
		await fireEvent.click(page.getByRole('button', { name: 'Agents' }));
		expect(page.getByText('reviewer')).toBeTruthy();
		await fireEvent.click(page.getByRole('button', { name: 'Skills' }));
		expect(page.getByText('lint')).toBeTruthy();
	});

	it('lists components this app does not run, with the reason, and nothing when all load', () => {
		const loaded = render(PluginDetails, { props: { plugin: installedDetails } });
		expect(loaded.queryByTestId('plugin-not-loaded')).toBeNull();
		cleanup();

		const plugin: PluginDetailsType = {
			...installedDetails,
			format: 'agent-plugins',
			components: [
				{ kind: 'skill', name: 'lint', support: 'loaded', reason: '' },
				{ kind: 'app', name: 'drive', support: 'unsupported', reason: 'hosted by ChatGPT; it cannot run outside it' },
				{ kind: 'user_config', name: 'api_token', support: 'needs_config', reason: 'asks for settings this app does not collect yet' }
			]
		};
		const page = render(PluginDetails, { props: { plugin } });
		const block = page.getByTestId('plugin-not-loaded');
		expect(block.textContent).toContain('App drive: hosted by ChatGPT; it cannot run outside it');
		expect(block.textContent).toContain('Settings api_token: asks for settings this app does not collect yet');
		expect(block.textContent).not.toContain('lint');
	});

	it('shows a marketplace plugin with an install action and no install date', async () => {
		const onInstall = vi.fn();
		const page = render(PluginDetails, { props: { plugin: marketplaceDetails, onInstall } });

		expect(page.queryByText(/^Installed /)).toBeNull();
		expect(page.queryByText(/1970/)).toBeNull();
		expect(page.queryByText('Hooks')).toBeNull();
		expect(page.getByText('No README available for this plugin')).toBeTruthy();
		await fireEvent.click(page.getByRole('button', { name: 'Install' }));
		expect(onInstall).toHaveBeenCalledOnce();
	});

	it('omits the install date for an installed record without one', () => {
		const page = render(PluginDetails, { props: { plugin: { ...installedDetails, installed_at: null } } });
		expect(page.queryByText(/^Installed /)).toBeNull();
		expect(page.queryByText(/1970/)).toBeNull();
		expect(page.getByText('Scope: user')).toBeTruthy();
	});
});
