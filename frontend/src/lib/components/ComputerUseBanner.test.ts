import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { computerUse } from '$lib/stores/computerUse.svelte';
import ComputerUseBanner from './ComputerUseBanner.svelte';

vi.mock('$lib/api/computerUse', () => ({ getComputerUseStatus: vi.fn(), setComputerUseEnabled: vi.fn(), stopComputerUse: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

beforeEach(() => {
	vi.spyOn(computerUse, 'start').mockImplementation(() => {});
	vi.spyOn(computerUse, 'stop').mockImplementation(() => {});
	Object.assign(computerUse, { active: true, displayLabel: 'Display 2 · 2560×1600',
		action: 'type', secondsSince: 0, busy: true, escapeAvailable: true,
		overlayAvailable: true, stopping: false, connectionLost: false, stopReason: null });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('ComputerUseBanner', () => {
	it('shows the target, current action, and a reachable Stop button', async () => {
		const halt = vi.spyOn(computerUse, 'halt').mockResolvedValue();
		render(ComputerUseBanner);
		expect(screen.getByText(/Display 2/)).toBeTruthy();
		expect(screen.getByText('Typing')).toBeTruthy();
		expect(screen.getByText(/Esc on host/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Stop computer use' }));
		expect(halt).toHaveBeenCalledTimes(1);
	});
	it('hides details without stopping control and keeps Stop available', async () => {
		const halt = vi.spyOn(computerUse, 'halt').mockResolvedValue();
		render(ComputerUseBanner);
		await fireEvent.click(screen.getByRole('button', { name: 'Hide computer use message' }));
		expect(screen.queryByText('Typing')).toBeNull();
		expect(screen.getByTestId('computer-use-banner').classList.contains('collapsed')).toBe(true);
		expect(computerUse.active).toBe(true);
		expect(halt).not.toHaveBeenCalled();
		computerUse.action = 'scroll';
		computerUse.secondsSince = 3;
		await tick();
		expect(screen.queryByText('Scrolling')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Show computer use details' }));
		expect(screen.getByText('Scrolling')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Hide computer use message' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Stop computer use' }));
		expect(halt).toHaveBeenCalledTimes(1);
	});
	it('shows stop feedback after hiding and expands for the next control period', async () => {
		render(ComputerUseBanner);
		await fireEvent.click(screen.getByRole('button', { name: 'Hide computer use message' }));
		computerUse.active = false;
		computerUse.stopReason = 'escape';
		await tick();
		expect(screen.getByText('Computer use stopped')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss computer use status' }));
		computerUse.active = true;
		computerUse.stopReason = null;
		await tick();
		expect(screen.getByText('Typing')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Hide computer use message' })).toBeTruthy();
	});
	it('keeps the control visible between actions', () => {
		computerUse.busy = false;
		computerUse.secondsSince = 90;
		render(ComputerUseBanner);
		expect(screen.getByText('Thinking · computer control is still active')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Stop computer use' })).toBeTruthy();
	});
	it('does not promise Escape when the native listener is unavailable', () => {
		computerUse.escapeAvailable = false;
		render(ComputerUseBanner);
		expect(screen.queryByText(/Esc on host/)).toBeNull();
		expect(screen.getByText(/Use Stop computer use/)).toBeTruthy();
	});
	it('confirms a stop and allows dismissing the confirmation', async () => {
		computerUse.active = false;
		computerUse.stopReason = 'escape';
		render(ComputerUseBanner);
		expect(screen.getByText('Computer use stopped')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss computer use status' }));
		expect(screen.queryByText('Computer use stopped')).toBeNull();
	});
});
