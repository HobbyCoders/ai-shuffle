/**
 * AI Review Tool
 *
 * Get a second opinion on code, plans, or content from another AI.
 * Useful for catching issues Claude might miss and getting diverse perspectives.
 *
 * Usage:
 *   import { aiReview } from './ai-collaborate/aiReview.js';
 *
 *   const result = await aiReview({
 *     content: 'function foo() { ... }',
 *     review_type: 'code',
 *     focus_areas: ['security', 'performance']
 *   });
 */
import { readFileSync, existsSync } from 'fs';
import { chatRegistry, } from '../providers/chat/index.js';
// ============================================================================
// Configuration
// ============================================================================
const DEFAULT_PROVIDER = 'openai';
const DEFAULT_MODEL = 'gpt-4o';
// ============================================================================
// Review Prompts
// ============================================================================
const REVIEW_PROMPTS = {
    code: `You are an expert code reviewer. Analyze the code for:
- Bugs and potential runtime errors
- Security vulnerabilities (injection, XSS, etc.)
- Performance issues
- Code smells and maintainability problems
- Missing error handling
- Edge cases not covered
- Best practices violations`,
    plan: `You are a strategic reviewer for technical plans. Analyze the plan for:
- Missing steps or dependencies
- Unrealistic estimates or assumptions
- Risk factors not addressed
- Alternative approaches not considered
- Potential blockers
- Unclear requirements
- Scalability concerns`,
    security: `You are a security expert. Analyze for:
- Authentication/authorization issues
- Data exposure risks
- Injection vulnerabilities (SQL, XSS, command)
- Insecure configurations
- Cryptographic weaknesses
- Information disclosure
- Access control problems
- OWASP Top 10 issues`,
    architecture: `You are a software architect. Analyze for:
- Design pattern misuse
- Coupling and cohesion issues
- Scalability limitations
- Single points of failure
- Technical debt accumulation
- Dependency problems
- API design issues
- Data modeling concerns`,
    documentation: `You are a documentation reviewer. Analyze for:
- Accuracy and completeness
- Clarity and readability
- Missing examples
- Outdated information
- Inconsistencies
- Organization issues
- Audience appropriateness`,
    general: `You are a thorough reviewer. Analyze for:
- Logical errors or inconsistencies
- Missing considerations
- Unclear areas
- Potential improvements
- Risks or concerns
- Best practices`
};
// ============================================================================
// Credential Resolution
// ============================================================================
function getCredentials(providerId) {
    switch (providerId) {
        case 'openai':
            return process.env.OPENAI_API_KEY ? { apiKey: process.env.OPENAI_API_KEY } : null;
        case 'google':
            const googleKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
            return googleKey ? { apiKey: googleKey } : null;
        case 'deepseek':
            return process.env.DEEPSEEK_API_KEY ? { apiKey: process.env.DEEPSEEK_API_KEY } : null;
        default:
            return null;
    }
}
// ============================================================================
// Review Functions
// ============================================================================
/**
 * Perform an AI review
 */
export async function aiReview(input) {
    const { content, review_type, focus_areas, context, reviewer } = input;
    if (!content || content.trim().length === 0) {
        return {
            success: false,
            approved: false,
            concerns: [],
            suggestions: [],
            error: 'Content is required for review'
        };
    }
    // Determine provider and model
    const providerId = reviewer || DEFAULT_PROVIDER;
    const provider = chatRegistry.get(providerId);
    if (!provider) {
        return {
            success: false,
            approved: false,
            concerns: [],
            suggestions: [],
            error: `Provider '${providerId}' not found. Available: ${chatRegistry.listIds().join(', ')}`
        };
    }
    const credentials = getCredentials(providerId);
    if (!credentials) {
        return {
            success: false,
            approved: false,
            concerns: [],
            suggestions: [],
            error: `No API key for provider '${providerId}'`
        };
    }
    // Build the prompt
    let systemPrompt = REVIEW_PROMPTS[review_type] || REVIEW_PROMPTS.general;
    if (focus_areas && focus_areas.length > 0) {
        systemPrompt += `\n\nFocus especially on: ${focus_areas.join(', ')}`;
    }
    systemPrompt += `\n\nRespond with a JSON object containing:
{
  "approved": boolean,
  "confidence": number (0-1),
  "concerns": [
    {
      "severity": "critical" | "major" | "minor" | "suggestion",
      "category": string,
      "description": string,
      "location": string (optional, line/section reference),
      "suggestion": string (optional, how to fix)
    }
  ],
  "strengths": [string],
  "suggestions": [string],
  "summary": string
}`;
    let userContent = content;
    if (context) {
        userContent = `## Context\n${context}\n\n## Content to Review\n${content}`;
    }
    const messages = [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userContent }
    ];
    try {
        const model = providerId === 'openai' ? DEFAULT_MODEL :
            providerId === 'google' ? 'gemini-2.5-flash-preview-05-20' :
                'deepseek-chat';
        const result = await provider.chat({
            messages,
            model,
            temperature: 0.3, // Lower temperature for consistent analysis
            response_format: { type: 'json_object' }
        }, credentials);
        if (!result.success) {
            return {
                success: false,
                approved: false,
                concerns: [],
                suggestions: [],
                error: result.error || 'Review failed'
            };
        }
        const responseText = typeof result.message?.content === 'string'
            ? result.message.content
            : '';
        // Parse the JSON response
        try {
            const parsed = JSON.parse(responseText);
            return {
                success: true,
                approved: parsed.approved ?? false,
                confidence: parsed.confidence,
                concerns: (parsed.concerns || []).map((c) => ({
                    severity: c.severity || 'minor',
                    category: c.category || 'general',
                    description: c.description || '',
                    location: c.location,
                    suggestion: c.suggestion
                })),
                strengths: parsed.strengths || [],
                suggestions: parsed.suggestions || [],
                summary: parsed.summary
            };
        }
        catch (parseError) {
            // If JSON parsing fails, try to extract information from text
            return {
                success: true,
                approved: !responseText.toLowerCase().includes('reject') &&
                    !responseText.toLowerCase().includes('critical'),
                concerns: [{
                        severity: 'minor',
                        category: 'parse_error',
                        description: 'Could not parse structured response',
                        suggestion: responseText.substring(0, 500)
                    }],
                suggestions: [],
                summary: responseText
            };
        }
    }
    catch (error) {
        return {
            success: false,
            approved: false,
            concerns: [],
            suggestions: [],
            error: error instanceof Error ? error.message : 'Unknown error'
        };
    }
}
/**
 * Review a file
 */
export async function reviewFile(filePath, options) {
    if (!existsSync(filePath)) {
        return {
            success: false,
            approved: false,
            concerns: [],
            suggestions: [],
            error: `File not found: ${filePath}`
        };
    }
    try {
        const content = readFileSync(filePath, 'utf-8');
        const ext = filePath.split('.').pop()?.toLowerCase();
        // Determine review type from extension
        let review_type = options?.review_type || 'general';
        if (!options?.review_type) {
            if (['ts', 'js', 'tsx', 'jsx', 'py', 'go', 'rs', 'java', 'c', 'cpp', 'h'].includes(ext || '')) {
                review_type = 'code';
            }
            else if (['md', 'mdx', 'rst', 'txt'].includes(ext || '')) {
                review_type = 'documentation';
            }
        }
        const filename = filePath.split('/').pop() || filePath;
        return aiReview({
            ...options,
            content: `// File: ${filename}\n\n${content}`,
            review_type,
            context: options?.context || `File: ${filePath}`
        });
    }
    catch (error) {
        return {
            success: false,
            approved: false,
            concerns: [],
            suggestions: [],
            error: `Failed to read file: ${error instanceof Error ? error.message : 'Unknown error'}`
        };
    }
}
/**
 * Quick security review
 */
export async function securityReview(content, context) {
    return aiReview({
        content,
        review_type: 'security',
        context,
        focus_areas: [
            'injection vulnerabilities',
            'authentication issues',
            'data exposure',
            'access control'
        ]
    });
}
/**
 * Get multiple reviews from different providers
 */
export async function multiReview(content, review_type = 'code', providers = ['openai', 'google']) {
    const reviews = await Promise.all(providers.map(async (provider) => {
        const result = await aiReview({
            content,
            review_type,
            reviewer: provider
        });
        return { provider, result };
    }));
    // Calculate consensus
    const successfulReviews = reviews.filter(r => r.result.success);
    if (successfulReviews.length === 0) {
        return {
            success: false,
            reviews
        };
    }
    const approvalCount = successfulReviews.filter(r => r.result.approved).length;
    const criticalCount = successfulReviews.reduce((sum, r) => sum + r.result.concerns.filter(c => c.severity === 'critical').length, 0);
    const majorCount = successfulReviews.reduce((sum, r) => sum + r.result.concerns.filter(c => c.severity === 'major').length, 0);
    return {
        success: true,
        reviews,
        consensus: {
            approved: approvalCount > successfulReviews.length / 2 && criticalCount === 0,
            criticalCount,
            majorCount
        }
    };
}
export default aiReview;
//# sourceMappingURL=aiReview.js.map