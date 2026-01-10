/**
 * Tests for AI generation API functions
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { generateProfile, generateSubagent, type AgentExport, type SubagentExport } from './client';

describe('AI Generation API', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		global.fetch = fetchMock as unknown as typeof fetch;
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	describe('generateProfile', () => {
		const mockProfileExport: AgentExport = {
			version: '1.0',
			type: 'ai-shuffle-agent',
			exported_at: '2024-01-15T12:00:00Z',
			agent: {
				name: 'Python Developer',
				description: 'A Python development profile',
				model: 'sonnet',
				permission_mode: 'default',
				system_prompt_type: 'preset',
				system_prompt_preset: 'claude_code',
				system_prompt_content: null,
				system_prompt_append: null,
				system_prompt_inject_env: false,
				allowed_tools: null,
				disallowed_tools: null,
				enabled_agents: null,
				ai_tools: null,
				max_turns: null,
				max_buffer_size: null,
				include_partial_messages: true,
				continue_conversation: false,
				fork_session: false,
				setting_sources: null,
				env: null
			}
		};

		it('should call POST /api/v1/generate/profile with description', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockProfileExport)
			});

			const result = await generateProfile('A Python developer profile');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/profile', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A Python developer profile' })
			});
			expect(result).toEqual(mockProfileExport);
		});

		it('should include model parameter when provided', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockProfileExport)
			});

			await generateProfile('A Python developer profile', 'opus');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/profile', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A Python developer profile', model: 'opus' })
			});
		});

		it('should throw ApiError on failure', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 500,
				json: () => Promise.resolve({ detail: 'AI generation failed' })
			});

			await expect(generateProfile('test')).rejects.toEqual({
				detail: 'AI generation failed',
				status: 500
			});
		});

		it('should throw ApiError on validation error', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 422,
				json: () => Promise.resolve({ detail: 'Description too short' })
			});

			await expect(generateProfile('short')).rejects.toEqual({
				detail: 'Description too short',
				status: 422
			});
		});

		it('should return correct AgentExport structure', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockProfileExport)
			});

			const result = await generateProfile('A profile for testing');

			expect(result.version).toBe('1.0');
			expect(result.type).toBe('ai-shuffle-agent');
			expect(result.exported_at).toBeDefined();
			expect(result.agent).toBeDefined();
			expect(result.agent.name).toBe('Python Developer');
			expect(result.agent.model).toBe('sonnet');
		});
	});

	describe('generateSubagent', () => {
		const mockSubagentExport: SubagentExport = {
			version: '1.0',
			type: 'ai-shuffle-subagent',
			exported_at: '2024-01-15T12:00:00Z',
			subagent: {
				id: 'code-reviewer',
				name: 'Code Reviewer',
				description: 'Use this agent to review code for bugs and style issues',
				prompt: 'You are an expert code reviewer...',
				tools: ['Read', 'Glob', 'Grep'],
				model: 'sonnet'
			}
		};

		it('should call POST /api/v1/generate/subagent with description', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockSubagentExport)
			});

			const result = await generateSubagent('A code review agent');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/subagent', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A code review agent' })
			});
			expect(result).toEqual(mockSubagentExport);
		});

		it('should include model parameter when provided', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockSubagentExport)
			});

			await generateSubagent('A code review agent', 'haiku');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/subagent', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A code review agent', model: 'haiku' })
			});
		});

		it('should throw ApiError on failure', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 500,
				json: () => Promise.resolve({ detail: 'AI generation failed' })
			});

			await expect(generateSubagent('test')).rejects.toEqual({
				detail: 'AI generation failed',
				status: 500
			});
		});

		it('should throw ApiError on validation error', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 422,
				json: () => Promise.resolve({ detail: 'Description too short' })
			});

			await expect(generateSubagent('short')).rejects.toEqual({
				detail: 'Description too short',
				status: 422
			});
		});

		it('should return correct SubagentExport structure', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockSubagentExport)
			});

			const result = await generateSubagent('A code reviewer agent');

			expect(result.version).toBe('1.0');
			expect(result.type).toBe('ai-shuffle-subagent');
			expect(result.exported_at).toBeDefined();
			expect(result.subagent).toBeDefined();
			expect(result.subagent.id).toBe('code-reviewer');
			expect(result.subagent.name).toBe('Code Reviewer');
			expect(result.subagent.description).toBeDefined();
			expect(result.subagent.prompt).toBeDefined();
			expect(result.subagent.tools).toEqual(['Read', 'Glob', 'Grep']);
			expect(result.subagent.model).toBe('sonnet');
		});

		it('should handle subagent with null tools and model', async () => {
			const exportWithNulls: SubagentExport = {
				...mockSubagentExport,
				subagent: {
					...mockSubagentExport.subagent,
					tools: null,
					model: null
				}
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(exportWithNulls)
			});

			const result = await generateSubagent('A basic agent');

			expect(result.subagent.tools).toBeNull();
			expect(result.subagent.model).toBeNull();
		});
	});

	describe('API error handling', () => {
		it('should handle network errors', async () => {
			fetchMock.mockRejectedValueOnce(new Error('Network error'));

			await expect(generateProfile('test description')).rejects.toThrow('Network error');
		});

		it('should handle unauthorized errors', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 401,
				json: () => Promise.resolve({ detail: 'Not authenticated' })
			});

			await expect(generateSubagent('test description')).rejects.toEqual({
				detail: 'Not authenticated',
				status: 401
			});
		});
	});
});
