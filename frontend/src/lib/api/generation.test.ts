/**
 * Tests for AI generation API functions
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	generateProfile,
	generateSubagent,
	continueGeneration,
	generateProfileDirect,
	generateSubagentDirect,
	type AgentExport,
	type SubagentExport,
	type GenerationResponse
} from './client';

describe('AI Generation API', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		global.fetch = fetchMock as unknown as typeof fetch;
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	// Mock data
	const mockProfileExport: AgentExport = {
		version: '1.0',
		type: 'ai-shuffle-agent',
		exported_at: '2024-01-15T12:00:00Z',
		agent: {
			name: 'Python Developer',
			description: 'A Python development profile',
			model: 'sonnet',
			permission_mode: 'default',
			thinking_mode: 'default',
			effort: null,
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
			enable_file_checkpointing: false,
			env: null,
			app_config_tools: null
		}
	};

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

	describe('generateProfile', () => {
		it('should call POST /api/v1/generate/profile with description', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockProfileExport,
				generation_type: 'profile'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			const result = await generateProfile('A Python developer profile');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/profile', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A Python developer profile' })
			});
			expect(result.status).toBe('complete');
			expect(result.result).toEqual(mockProfileExport);
		});

		it('should include model parameter when provided', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockProfileExport,
				generation_type: 'profile'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			await generateProfile('A Python developer profile', 'opus');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/profile', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A Python developer profile', model: 'opus' })
			});
		});

		it('should include skip_clarification when provided', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockProfileExport,
				generation_type: 'profile'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			await generateProfile('A Python developer profile', 'sonnet', true);

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/profile', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					description: 'A Python developer profile',
					model: 'sonnet',
					skip_clarification: true
				})
			});
		});

		it('should return clarification questions when needed', async () => {
			const response: GenerationResponse = {
				status: 'needs_clarification',
				questions: [
					{
						id: 'vague_purpose',
						question: 'What specific tasks should this agent handle?',
						options: ['Code review', 'Testing', 'Documentation'],
						context: 'More detail helps generate better prompts'
					}
				],
				generation_type: 'profile'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			const result = await generateProfile('A helpful tool');

			expect(result.status).toBe('needs_clarification');
			expect(result.questions).toBeDefined();
			expect(result.questions!.length).toBeGreaterThan(0);
			expect(result.questions![0].id).toBe('vague_purpose');
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
	});

	describe('generateSubagent', () => {
		it('should call POST /api/v1/generate/subagent with description', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockSubagentExport,
				generation_type: 'subagent'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			const result = await generateSubagent('A code review agent');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/subagent', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A code review agent' })
			});
			expect(result.status).toBe('complete');
			expect(result.result).toEqual(mockSubagentExport);
		});

		it('should include model parameter when provided', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockSubagentExport,
				generation_type: 'subagent'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			await generateSubagent('A code review agent', 'haiku');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/subagent', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A code review agent', model: 'haiku' })
			});
		});

		it('should return clarification questions when needed', async () => {
			const response: GenerationResponse = {
				status: 'needs_clarification',
				questions: [
					{
						id: 'too_short',
						question: 'Could you provide more detail?'
					}
				],
				generation_type: 'subagent'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			const result = await generateSubagent('A tool');

			expect(result.status).toBe('needs_clarification');
			expect(result.questions).toBeDefined();
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
	});

	describe('continueGeneration', () => {
		it('should call POST /api/v1/generate/continue with answers', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockProfileExport,
				generation_type: 'profile'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			const result = await continueGeneration('profile', 'A helpful tool', {
				vague_purpose: 'Code review'
			});

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/continue', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					generation_type: 'profile',
					description: 'A helpful tool',
					answers: { vague_purpose: 'Code review' }
				})
			});
			expect(result.status).toBe('complete');
		});

		it('should include model when provided', async () => {
			const response: GenerationResponse = {
				status: 'complete',
				result: mockSubagentExport,
				generation_type: 'subagent'
			};

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(response)
			});

			await continueGeneration(
				'subagent',
				'A tool',
				{ purpose: 'Security review' },
				'opus'
			);

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/continue', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					generation_type: 'subagent',
					description: 'A tool',
					answers: { purpose: 'Security review' },
					model: 'opus'
				})
			});
		});
	});

	describe('generateProfileDirect (legacy)', () => {
		it('should call POST /api/v1/generate/profile/direct', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockProfileExport)
			});

			const result = await generateProfileDirect('A Python developer profile');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/profile/direct', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A Python developer profile' })
			});
			expect(result.version).toBe('1.0');
			expect(result.type).toBe('ai-shuffle-agent');
			expect(result.agent.name).toBe('Python Developer');
		});
	});

	describe('generateSubagentDirect (legacy)', () => {
		it('should call POST /api/v1/generate/subagent/direct', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockSubagentExport)
			});

			const result = await generateSubagentDirect('A code review agent');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/generate/subagent/direct', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ description: 'A code review agent' })
			});
			expect(result.version).toBe('1.0');
			expect(result.type).toBe('ai-shuffle-subagent');
			expect(result.subagent.id).toBe('code-reviewer');
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
