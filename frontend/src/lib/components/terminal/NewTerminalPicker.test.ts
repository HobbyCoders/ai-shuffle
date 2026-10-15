import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import NewTerminalPicker from './NewTerminalPicker.svelte';
import type { ProbeState } from '$lib/terminal/gating';

afterEach(cleanup);

it('does not silently retarget a selection when the target list changes', async () => {
    const onOpen = vi.fn();
    const ui = render(NewTerminalPicker, { probe, onOpen });
    await fireEvent.change(ui.getByLabelText('Target'), { target: { value: 'worker:peer' } });
    await ui.rerender({ probe: { ...probe, targets: [probe.targets![0], probe.targets![2]] }, onOpen });
    expect((ui.getByRole('button', { name: 'Open terminal' }) as HTMLButtonElement).disabled).toBe(true);
    expect(onOpen).not.toHaveBeenCalled();
});
const probe: Extract<ProbeState, { status: 'ready' }> = {
    status: 'ready', supported: true, reason: null, worker: 'Local', shells: ['bash', 'sh'],
    defaultShell: 'bash', folder: '/local/project', folderSource: 'project', open: 0, limit: 8,
    targets: [
        { kind: 'worker', worker_id: 'local', name: 'Local', available: true },
        { kind: 'worker', worker_id: 'peer', name: 'Peer', available: true },
        { kind: 'ssh', grant_id: 'grant', name: 'Approved host', available: true },
    ],
};

it('requires explicit peer selection and never forwards the local folder or shell default', async () => {
    const onOpen = vi.fn();
    const ui = render(NewTerminalPicker, { probe, onOpen });
    await fireEvent.change(ui.getByLabelText('Target'), { target: { value: 'worker:peer' } });
    expect((ui.getByRole('textbox') as HTMLInputElement).value).toBe('');
    await fireEvent.click(ui.getByRole('button', { name: 'Open terminal' }));
    expect(onOpen).toHaveBeenCalledWith({ cwd: null, shell: null, target: { kind: 'worker', worker_id: 'peer', grant_id: undefined } });
});

it('selects an SSH grant without asking for credentials or permitting a shell command', async () => {
    const onOpen = vi.fn();
    const ui = render(NewTerminalPicker, { probe, onOpen });
    await fireEvent.change(ui.getByLabelText('Target'), { target: { value: 'ssh:grant' } });
    expect(ui.queryByRole('textbox')).toBeNull();
    expect(ui.getByText(/Revoking the grant closes/)).toBeTruthy();
    await fireEvent.click(ui.getByRole('button', { name: 'Open terminal' }));
    expect(onOpen).toHaveBeenCalledWith({ cwd: null, shell: null, target: { kind: 'ssh', grant_id: 'grant', worker_id: undefined } });
});
