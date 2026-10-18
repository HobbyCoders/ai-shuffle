import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ProjectGeneralTab from './ProjectGeneralTab.svelte';
import { getGitStatus, initRepository } from '$lib/api/git';
import { git } from '$lib/stores/git.svelte';

vi.mock('$lib/stores/app-data.svelte', async () => {
    const { writable } = await import('svelte/store');
    return { projects: writable([{ id: 'project', name: 'Plain folder', path: '/plain' }]), appData: { updateProject: vi.fn() } };
});
vi.mock('$lib/stores/groups.svelte', async () => {
    const { writable } = await import('svelte/store');
    return { groups: writable({ projects: { groups: [], assignments: {} } }) };
});
vi.mock('$lib/stores/git.svelte', () => ({ git: { refreshStatus: vi.fn() } }));
vi.mock('$lib/api/git', () => ({ getGitStatus: vi.fn(), initRepository: vi.fn(), cloneRepository: vi.fn() }));
vi.mock('$lib/components/git/GitHubRepoPicker.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

afterEach(cleanup);
beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getGitStatus).mockResolvedValue({ is_git_repo: false } as any);
    vi.mocked(initRepository).mockResolvedValue({ success: true } as any);
});

describe('project-management Git initialization', () => {
    it('offers init outside the chat header and reloads capability after the real modal submits', async () => {
        render(ProjectGeneralTab, { projectId: 'project' });
        await fireEvent.click(await screen.findByRole('button', { name: 'Initialize Git repository' }));
        expect(screen.getByRole('button', { name: /clone/i })).toBeTruthy();
        vi.mocked(getGitStatus).mockResolvedValue({ is_git_repo: true } as any);
        await fireEvent.click(screen.getByRole('button', { name: /^initialize$/i }));
        await waitFor(() => expect(initRepository).toHaveBeenCalledWith('project', expect.objectContaining({ create_initial_commit: true }), undefined));
        expect(git.refreshStatus).toHaveBeenCalledOnce();
        await waitFor(() => expect(screen.queryByRole('button', { name: 'Initialize Git repository' })).toBeNull());
    });
    it.each(['repository', 'unavailable'])('does not offer initialization for %s', async state => {
        if (state === 'repository') vi.mocked(getGitStatus).mockResolvedValue({ is_git_repo: true } as any);
        else vi.mocked(getGitStatus).mockRejectedValue({ status: 503, detail: 'Git checkout is unavailable' });
        render(ProjectGeneralTab, { projectId: 'project' });
        await waitFor(() => expect(getGitStatus).toHaveBeenCalledWith('project'));
        expect(screen.queryByRole('button', { name: 'Initialize Git repository' })).toBeNull();
    });
});
