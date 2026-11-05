import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet } from 'svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import DetailPane from './DetailPane.svelte';
import EmptyIdentityFixture from '../../../../../tests/EmptyIdentityFixture.svelte';

// Raw snippets render once; the tab state is asserted on the tabs and the store.
const body = createRawSnippet((tab: () => string | null) => ({
	render: () => `<p data-testid="body">Tab: ${tab() ?? 'none'}</p>`
}));

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	settingsNav.reset();
	settingsNav.selectCategory('providers');
	settingsNav.selectItem('c-sub');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('DetailPane', () => {
 it('removes the identity grid column when leaving a custom avatar for an anonymous header', async () => {
  const tile = createRawSnippet(() => ({ render: () => '<span data-testid="avatar">AV</span>' }));
  const view = render(DetailPane, { name: 'Agent', tile, contentFirst: true });
  expect(view.container.querySelector('.st-dh:has(> .st-identity > *)')).toBeTruthy();
  expect(screen.getByTestId('avatar')).toBeTruthy();
  await view.rerender({ name: 'Workspace', tile: undefined });
  expect(view.container.querySelector('.st-dh:has(> .st-identity > *)')).toBeNull();
  expect(screen.queryByTestId('avatar')).toBeNull();
 });
 it('does not treat an intentionally empty tile snippet as identity art', () => {
  const view = render(EmptyIdentityFixture);
  expect(view.container.querySelector('.st-identity')?.children.length).toBe(0);
  expect(view.container.querySelector('.st-dh:has(> .st-identity > *)')).toBeNull();
 });
 it('does not reserve an identity column without identity art, and preserves a provider glyph', async () => {
  const view = render(DetailPane, { name: 'Workspace', identity: true, contentFirst: true });
  expect(view.container.querySelector('.st-dh:has(> .st-identity > *)')).toBeNull();
  expect(view.container.querySelector('.st-tile')).toBeNull();
  await view.rerender({ name: 'Provider', identity: false, glyph: 'C' });
  expect(view.container.querySelector('.st-dh:has(> .st-identity > *)')).toBeTruthy();
  expect(view.container.querySelector('.st-tile')?.textContent?.trim()).toBe('C');
 });
 it('omits the entire Back row for content-first pages, not just its button', () => {
  const { container } = render(DetailPane, { name: 'Stats', contentFirst: true });
  expect(container.querySelector('.st-back-row')).toBeNull();
  expect(container.querySelector('h2')?.textContent).toBe('Stats');
 });

	it('opts collection pages into the centred reading column without changing Settings defaults', () => {
		const normal = render(DetailPane, { name: 'Settings detail' });
		expect(normal.container.querySelector('.st-dwrap.reading')).toBeNull();
		normal.unmount();
		const centered = render(DetailPane, { name: 'Project detail', width: 'reading' });
		expect(centered.container.querySelector('.st-dwrap.reading')).toBeTruthy();
	});
	it('uses the shared frame and an explicit Docs width', () => {
		const { container } = render(DetailPane, { name: 'Remote access', width: 'docs', backLabel: 'Docs' });
		expect(container.querySelector('.st-dwrap.docs')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Back to Docs' })).toBeTruthy();
		expect(container.querySelector('.st-dh')?.classList.contains('has-identity')).toBe(false);
	});
	it('renders header, at most four facts with data, tabs and the danger zone', async () => {
		const onConfirm = vi.fn();
		render(DetailPane, {
			name: 'Claude Max subscription',
			glyph: 'C',
			status: 'ok',
			statusText: 'Active for Claude',
			badge: 'Subscription',
			facts: [
				{ label: 'Account', value: 'qk@example.dev' },
				{ label: 'Plan', value: 'Max' },
				{ label: 'Last used', value: null },
				{ label: 'Models', value: 4 },
				{ label: 'Version', value: '2.1' },
				{ label: 'Extra', value: 'x' }
			],
			tabs: ['Overview', 'Models', 'Usage', 'Policy'],
			danger: [{ label: 'Remove', button: 'Remove', question: 'Remove this credential?', onConfirm }],
			children: body
		});
		expect(screen.getByRole('heading', { level: 2, name: 'Claude Max subscription' })).toBeTruthy();
		expect(screen.getByText('Subscription')).toBeTruthy();
		expect(screen.getByText('Active for Claude')).toBeTruthy();
		const facts = screen.getAllByRole('term').map((t) => t.textContent);
		expect(facts).toEqual(['Account', 'Plan', 'Models', 'Version']);

		const tabs = screen.getByRole('tablist', { name: 'Claude Max subscription sections' });
		expect(within(tabs).getByRole('tab', { name: 'Overview' }).getAttribute('aria-selected')).toBe('true');
		expect(screen.getByTestId('body').textContent).toBe('Tab: Overview');
		await fireEvent.click(within(tabs).getByRole('tab', { name: 'Policy' }));
		expect(settingsNav.tabOf('providers', 'c-sub', ['Overview', 'Policy'])).toBe('Policy');
		expect(within(tabs).getByRole('tab', { name: 'Policy' }).getAttribute('aria-selected')).toBe('true');
		expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe(
			within(tabs).getByRole('tab', { name: 'Policy' }).id
		);
		await fireEvent.keyDown(tabs, { key: 'ArrowLeft' });
		expect(within(tabs).getByRole('tab', { name: 'Usage' }).getAttribute('aria-selected')).toBe('true');

		expect(screen.getByRole('heading', { name: 'Danger zone' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		expect(onConfirm).toHaveBeenCalledOnce();
	});

	it('skips tabs when there is only one, and disables the body when read-only', () => {
		const { container } = render(DetailPane, {
			props: { name: 'Media key', tabs: ['Overview'], children: body },
			context: new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]])
		});
		expect(screen.queryByRole('tablist')).toBeNull();
		expect(screen.getByTestId('body').textContent).toBe('Tab: Overview');
		expect(container.querySelector('fieldset.st-dbody')?.hasAttribute('disabled')).toBe(true);
	});
});
