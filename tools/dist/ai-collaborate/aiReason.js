/**
 * AI Reason Tool
 *
 * Leverages reasoning-optimized models (DeepSeek-R1, o1) for complex
 * logical analysis, verification, and problem-solving.
 *
 * Usage:
 *   import { aiReason } from './ai-collaborate/aiReason.js';
 *
 *   const result = await aiReason({
 *     problem: 'Is this algorithm O(n log n)?',
 *     proposed_solution: 'Yes because...',
 *     verify: true
 *   });
 */
import { chatRegistry, } from '../providers/chat/index.js';
// ============================================================================
// Configuration
// ============================================================================
// Prefer DeepSeek for reasoning (better price/performance for reasoning)
// Fall back to OpenAI o1 if DeepSeek unavailable
const REASONING_PROVIDERS = [
    { id: 'deepseek', model: 'deepseek-reasoner' },
    { id: 'openai', model: 'o1-mini' }
];
// ============================================================================
// Credential Resolution
// ============================================================================
function getCredentials(providerId) {
    switch (providerId) {
        case 'deepseek':
            return process.env.DEEPSEEK_API_KEY ? { apiKey: process.env.DEEPSEEK_API_KEY } : null;
        case 'openai':
            return process.env.OPENAI_API_KEY ? { apiKey: process.env.OPENAI_API_KEY } : null;
        default:
            return null;
    }
}
/**
 * Find an available reasoning provider
 */
function getReasoningProvider() {
    for (const { id, model } of REASONING_PROVIDERS) {
        const provider = chatRegistry.get(id);
        const credentials = getCredentials(id);
        if (provider && credentials) {
            return { providerId: id, model, credentials };
        }
    }
    return null;
}
// ============================================================================
// Reasoning Functions
// ============================================================================
/**
 * Perform logical reasoning and analysis
 */
export async function aiReason(input) {
    const { problem, proposed_solution, verify, constraints, context } = input;
    if (!problem || problem.trim().length === 0) {
        return {
            success: false,
            reasoning: '',
            conclusion: '',
            confidence: 0,
            error: 'Problem statement is required'
        };
    }
    // Find an available reasoning provider
    const reasoner = getReasoningProvider();
    if (!reasoner) {
        return {
            success: false,
            reasoning: '',
            conclusion: '',
            confidence: 0,
            error: 'No reasoning provider available. Set DEEPSEEK_API_KEY or OPENAI_API_KEY.'
        };
    }
    const provider = chatRegistry.get(reasoner.providerId);
    if (!provider) {
        return {
            success: false,
            reasoning: '',
            conclusion: '',
            confidence: 0,
            error: `Provider ${reasoner.providerId} not found`
        };
    }
    // Build the prompt
    let systemPrompt = `You are a logical reasoning engine. Your task is to think through problems step-by-step, identify assumptions, consider alternatives, and reach well-justified conclusions.

IMPORTANT:
- Show your reasoning process explicitly
- Break down complex problems into steps
- Identify and validate assumptions
- Consider edge cases
- Quantify your confidence (0-1 scale)
- Be honest about uncertainty`;
    let userPrompt = `## Problem\n\n${problem}`;
    if (context) {
        userPrompt = `## Context\n\n${context}\n\n${userPrompt}`;
    }
    if (constraints && constraints.length > 0) {
        userPrompt += `\n\n## Constraints\n\n${constraints.map(c => `- ${c}`).join('\n')}`;
    }
    if (verify && proposed_solution) {
        systemPrompt += `\n\nYou are being asked to VERIFY a proposed solution. Your job is to:
1. Understand the proposed solution
2. Check if the reasoning is sound
3. Look for logical flaws or gaps
4. Identify edge cases the solution might miss
5. Determine if the conclusion is valid`;
        userPrompt += `\n\n## Proposed Solution to Verify\n\n${proposed_solution}`;
    }
    userPrompt += `\n\n## Instructions\n\nAnalyze this ${verify ? 'solution' : 'problem'} and respond with a JSON object:
{
  "reasoning": "Your step-by-step reasoning process",
  "conclusion": "Your final conclusion (1-2 sentences)",
  "confidence": 0.0 to 1.0,
  "flaws_found": ["List of logical flaws if verifying, or empty array"],
  "alternatives": ["Alternative approaches considered"]
}`;
    const messages = [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userPrompt }
    ];
    try {
        // Note: Reasoning models (o1, deepseek-reasoner) don't support JSON mode
        // So we'll parse the response ourselves
        const result = await provider.chat({
            messages,
            model: reasoner.model,
            // Reasoning models typically don't support temperature or other sampling params
            max_tokens: 4000
        }, reasoner.credentials);
        if (!result.success) {
            return {
                success: false,
                reasoning: '',
                conclusion: '',
                confidence: 0,
                error: result.error || 'Reasoning failed'
            };
        }
        const responseText = typeof result.message?.content === 'string'
            ? result.message.content
            : '';
        // Try to parse as JSON
        try {
            // Extract JSON from response (might be wrapped in markdown)
            const jsonMatch = responseText.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
                const parsed = JSON.parse(jsonMatch[0]);
                return {
                    success: true,
                    reasoning: parsed.reasoning || responseText,
                    conclusion: parsed.conclusion || '',
                    confidence: typeof parsed.confidence === 'number' ? parsed.confidence : 0.7,
                    flaws_found: parsed.flaws_found || [],
                    alternatives: parsed.alternatives || []
                };
            }
        }
        catch {
            // JSON parsing failed, extract what we can from text
        }
        // Fallback: parse from text
        return {
            success: true,
            reasoning: responseText,
            conclusion: extractConclusion(responseText),
            confidence: estimateConfidence(responseText),
            flaws_found: verify ? extractFlaws(responseText) : [],
            alternatives: extractAlternatives(responseText)
        };
    }
    catch (error) {
        return {
            success: false,
            reasoning: '',
            conclusion: '',
            confidence: 0,
            error: error instanceof Error ? error.message : 'Unknown error'
        };
    }
}
// ============================================================================
// Text Extraction Helpers
// ============================================================================
/**
 * Extract conclusion from reasoning text
 */
function extractConclusion(text) {
    // Look for conclusion markers
    const markers = [
        /(?:conclusion|therefore|thus|in summary|to summarize|finally)[:\s]+(.+?)(?:\n|$)/i,
        /(?:the answer is|my conclusion is)[:\s]+(.+?)(?:\n|$)/i
    ];
    for (const pattern of markers) {
        const match = text.match(pattern);
        if (match) {
            return match[1].trim().substring(0, 500);
        }
    }
    // Fallback: last paragraph
    const paragraphs = text.split(/\n\n+/).filter(p => p.trim());
    if (paragraphs.length > 0) {
        const last = paragraphs[paragraphs.length - 1];
        return last.trim().substring(0, 500);
    }
    return '';
}
/**
 * Estimate confidence from text
 */
function estimateConfidence(text) {
    const lowConfidence = /(?:uncertain|unsure|might|possibly|unclear|not sure|hard to say)/i;
    const highConfidence = /(?:certain|definitely|clearly|obviously|without doubt|confident)/i;
    const mediumConfidence = /(?:likely|probably|appears|seems|suggests)/i;
    if (highConfidence.test(text))
        return 0.85;
    if (lowConfidence.test(text))
        return 0.4;
    if (mediumConfidence.test(text))
        return 0.65;
    return 0.6; // Default moderate confidence
}
/**
 * Extract flaws from verification text
 */
function extractFlaws(text) {
    const flaws = [];
    // Look for flaw indicators
    const patterns = [
        /(?:flaw|issue|problem|error|mistake|incorrect|wrong)[:\s]+(.+?)(?:\n|$)/gi,
        /(?:however|but)[,\s]+(.+?)(?:\n|$)/gi
    ];
    for (const pattern of patterns) {
        let match;
        while ((match = pattern.exec(text)) !== null) {
            const flaw = match[1].trim();
            if (flaw.length > 10 && flaw.length < 500) {
                flaws.push(flaw);
            }
        }
    }
    return flaws.slice(0, 5); // Limit to 5 flaws
}
/**
 * Extract alternatives from text
 */
function extractAlternatives(text) {
    const alternatives = [];
    const patterns = [
        /(?:alternative|another approach|instead|alternatively)[:\s]+(.+?)(?:\n|$)/gi,
        /(?:could also|might also|another option)[:\s]+(.+?)(?:\n|$)/gi
    ];
    for (const pattern of patterns) {
        let match;
        while ((match = pattern.exec(text)) !== null) {
            const alt = match[1].trim();
            if (alt.length > 10 && alt.length < 300) {
                alternatives.push(alt);
            }
        }
    }
    return alternatives.slice(0, 3); // Limit to 3 alternatives
}
// ============================================================================
// Convenience Functions
// ============================================================================
/**
 * Verify a proposed solution
 */
export async function verifySolution(problem, solution, constraints) {
    return aiReason({
        problem,
        proposed_solution: solution,
        verify: true,
        constraints
    });
}
/**
 * Analyze a problem and propose solutions
 */
export async function analyzeProblem(problem, context, constraints) {
    return aiReason({
        problem,
        context,
        constraints,
        verify: false
    });
}
/**
 * Check if reasoning is sound
 */
export async function checkLogic(argument) {
    const result = await aiReason({
        problem: 'Is the following argument logically sound?',
        proposed_solution: argument,
        verify: true
    });
    return {
        valid: result.success && result.flaws_found?.length === 0 && result.confidence > 0.7,
        flaws: result.flaws_found || [],
        confidence: result.confidence
    };
}
/**
 * Get a reasoned answer with confidence
 */
export async function reasonedAnswer(question, context) {
    const result = await aiReason({
        problem: question,
        context,
        verify: false
    });
    return {
        answer: result.conclusion,
        reasoning: result.reasoning,
        confidence: result.confidence
    };
}
export default aiReason;
//# sourceMappingURL=aiReason.js.map