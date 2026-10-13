"""
Master Generation Prompts for AI Agent/Subagent Creation

Contains the comprehensive prompts used by Claude to generate
professional-quality agent and subagent configurations.
"""

from typing import Optional

from app.core.generation.examples import (
    get_subagent_examples_text,
    get_profile_examples_text,
)
from app.core.generation.templates import (
    DOMAIN_TEMPLATES,
)
from app.core.generation.tool_mapping import (
    get_all_available_tools,
)


# ============================================================================
# System Prompt Structure Guidelines
# ============================================================================

SYSTEM_PROMPT_STRUCTURE_GUIDE = '''
## System Prompt Best Practices

A high-quality system prompt should include these sections (adapt as appropriate):

### 1. Core Identity & Purpose (Required)
- Clear role statement: "You are a [Role Name], specialized in [specific domain]"
- Primary responsibilities as bullet points
- Scope boundaries (what the agent should NOT do)

### 2. Methodology/Workflow (Required for complex agents)
- Step-by-step approach to tasks
- Decision-making framework
- Prioritization guidelines

### 3. Tool Usage Guidelines (Required if tools are restricted)
- When to use each available tool
- Tool selection criteria
- Efficiency guidelines (which tool for which task)

### 4. Output Format (Recommended)
- Structure for responses (markdown, JSON, etc.)
- Templates for common outputs
- Examples of good output

### 5. Communication Style (Optional)
- Tone and voice guidelines
- Verbosity level
- Technical depth

### 6. Examples (Recommended for complex domains)
- Sample inputs and outputs
- Edge case handling
- Before/after comparisons

### 7. Anti-Patterns (Recommended)
- Common mistakes to avoid
- What NOT to do
- Known pitfalls

### 8. Safety Constraints (Required for sensitive domains)
- Boundaries and limitations
- Escalation criteria
- Security considerations
'''


# ============================================================================
# Subagent Generation Prompt
# ============================================================================

def build_subagent_generation_prompt(
    description: str,
    clarification_context: Optional[str] = None,
    reference_material: Optional[str] = None,
) -> str:
    """
    Build the comprehensive prompt for subagent generation.

    Args:
        description: User's description of the desired subagent
        clarification_context: Additional context from clarification answers
        reference_material: Optional reference material (existing prompts, examples)

    Returns:
        Complete prompt string for Claude
    """
    available_tools = get_all_available_tools()

    clarification_section = ""
    if clarification_context:
        clarification_section = f'''
## Additional Context from User
{clarification_context}

Use this context to refine your generation.
'''

    reference_section = ""
    if reference_material:
        reference_section = f'''
## Reference Material Provided by User

The user has provided the following reference material (existing prompts, examples, or detailed specifications).
Analyze this material and incorporate relevant patterns, terminology, and approaches into your generated configuration.
If this contains an existing system prompt, use it as inspiration while improving structure and clarity.

<reference_material>
{reference_material}
</reference_material>

'''

    return f'''You are an expert AI agent designer. Your task is to generate a complete, professional-quality subagent configuration based on the user's description.

## What Are Subagents?

Subagents are specialized AI assistants that the main Claude agent can delegate tasks to using the "Agent" tool (formerly "Task"). They run autonomously and return results to the main agent. Key characteristics:

- Invoked via: `Agent(subagent_type="<id>", prompt="<task description>")`
- Run with their own system prompt and tool restrictions
- Return a single response to the main agent
- Should be specialists focused on specific domains

## Configuration Fields

### Required Fields
- **id**: Unique identifier (lowercase letters, numbers, hyphens only)
  - Good: "code-reviewer", "test-writer-python", "security-auditor"
  - Bad: "Code Reviewer", "my_agent", "agent123!"

- **name**: Human-readable display name (1-100 chars)

- **description**: CRITICAL - This tells the main agent WHEN to use this subagent
  - Be specific about use cases and triggers
  - Include example scenarios
  - Mention what problems this agent solves

- **prompt**: The system prompt - detailed instructions for the subagent
  - Should be comprehensive (500-2000 words typically)
  - Follow the structure guidelines below
  - Include tool usage instructions if tools are restricted

### Optional Fields
- **tools**: Array of tool names this subagent can use
  - If null/omitted: inherits all tools from parent
  - Restrict tools for security/focus when appropriate
  - Available tools: {available_tools}

- **model**: Model override for this subagent
  - "haiku": Fast, cheap - use for simple searches, summaries, quick tasks
  - "sonnet": Balanced - use for most analysis, coding, moderate complexity
  - "opus": Powerful - complex reasoning, architecture, security analysis
  - "fable": Most capable - hardest reasoning, security, long-running tasks
  (sonnet/opus/fable all run a 1M context window natively - there is no suffix
  or flag to opt into it)
  - null: Inherit from parent (default)
  - Any other string is treated as a custom model ID and passed through unchanged

## Domain Templates

I recognize these common agent patterns. If the description matches one, use its configuration as a starting point:

{_format_domain_templates()}

## Tool Selection Logic

Match tools to agent purpose:

| Domain | Recommended Tools | Rationale |
|--------|------------------|-----------|
| Code Review | Read, Grep, Glob | Read-only analysis |
| Testing | Read, Write, Edit, Bash, Grep, Glob | Needs to write and run tests |
| Documentation | Read, Write, Edit, Glob | Needs to write docs |
| Security | Read, Grep, Glob, WebSearch | Analysis + CVE lookup |
| Research | Read, Grep, Glob, WebFetch, WebSearch | Information gathering |
| Exploration | Read, Grep, Glob | Fast codebase search |
| Planning | Read, Grep, Glob | Analysis for planning |

**Security principle**: Grant minimum necessary tools. Don't give Write/Edit to agents that only analyze.

{SYSTEM_PROMPT_STRUCTURE_GUIDE}

## Few-Shot Examples

Here are high-quality examples of subagent configurations:

{get_subagent_examples_text()}

## User's Description
{description}
{clarification_section}{reference_section}## Output Format

Analyze the user's description, identify the domain/purpose, and generate a complete configuration.

Output ONLY a valid JSON object (no markdown code blocks, no explanation):

{{
  "id": "descriptive-kebab-case-id",
  "name": "Human Readable Name",
  "description": "Clear description of when to invoke this agent. Be specific about use cases.",
  "tools": ["Read", "Grep", "Glob"],
  "model": "sonnet",
  "prompt": "Comprehensive system prompt following the structure guidelines above..."
}}

## Quality Checklist

Before outputting, verify:
- [ ] id is lowercase with hyphens only
- [ ] description clearly states WHEN to use the agent
- [ ] prompt has clear structure with sections
- [ ] prompt includes tool usage guidelines if tools are restricted
- [ ] prompt includes output format expectations
- [ ] model matches task complexity (haiku for simple, opus for complex)
- [ ] tools are minimized to what's actually needed

Generate the subagent configuration now:'''


# ============================================================================
# Profile Generation Prompt
# ============================================================================

def build_profile_generation_prompt(
    description: str,
    clarification_context: Optional[str] = None,
    reference_material: Optional[str] = None,
) -> str:
    """
    Build the comprehensive prompt for profile generation.

    Args:
        description: User's description of the desired profile
        clarification_context: Additional context from clarification answers
        reference_material: Optional reference material (existing prompts, examples)

    Returns:
        Complete prompt string for Claude
    """
    available_tools = get_all_available_tools()

    clarification_section = ""
    if clarification_context:
        clarification_section = f'''
## Additional Context from User
{clarification_context}

Use this context to refine your generation.
'''

    reference_section = ""
    if reference_material:
        reference_section = f'''
## Reference Material Provided by User

The user has provided the following reference material (existing prompts, examples, or detailed specifications).
Analyze this material and incorporate relevant patterns, terminology, and approaches into your generated configuration.
If this contains an existing system prompt, use it as the basis for system_prompt_content and improve it.

<reference_material>
{reference_material}
</reference_material>

'''

    return f'''You are an expert AI agent designer. Your task is to generate a complete Claude profile configuration based on the user's description.

## What Are Profiles?

Profiles are pre-configured settings for Claude sessions. They control:
- Available tools
- System prompt customization
- Memory and delegated subagents

They do NOT choose a model or a permission level: the person starting a chat
picks those for that chat.

## Configuration Fields (All Fields)

### Identity
- **name**: Display name for the profile (1-100 chars)
- **description**: Brief description of the profile's purpose

### Tool Configuration
- **allowed_tools**: If set, ONLY these tools are available (whitelist)
- **disallowed_tools**: These tools are blocked (blacklist)
- Available tools: {available_tools}

### System Prompt
- **system_prompt_content**: Full custom system prompt for the profile
  (null = use the standard coding-assistant prompt). This is the ONLY
  system-prompt field - there are no presets or append fields.

### Streaming
- **include_partial_messages**: Enable streaming partial messages (default: true)

### Advanced
- **enabled_agents**: Array of subagent IDs this profile can use
- **memory**: Durable text injected whenever this agent runs (usually null
  for a fresh profile - the agent writes its own memory over time)
- **app_config_tools**: Allow the agent to manage this app's own configuration
  via tools (default: false)
- **env**: Environment variables to set (key-value pairs)

### AI Tools (for creative profiles)
- **ai_tools**: Object enabling AI generation capabilities:
  - image_generation: Generate images from text
  - image_editing: Edit existing images
  - video_generation: Generate videos
  - image_to_video: Animate images
  - text_to_3d: Generate 3D models
  - (and more)

## Common Profile Patterns

| Use Case | Model | Permission | Tools |
|----------|-------|------------|-------|
| Quick Questions | haiku | default | all |
| Code Review | sonnet | default | Read, Grep, Glob only |
| Development | sonnet | acceptEdits | all |
| Automation | sonnet | default | all (approve risky tools, or use a narrow allow-list) |
| Architecture | opus | default | Read, Grep, Glob |
| Creative Work | sonnet | acceptEdits | all + AI tools |

## System Prompt Guidelines

When generating custom system prompt content:
- Keep focused on the profile's specific purpose
- Add domain-specific guidelines
- Include output format preferences if relevant
- Reference tool restrictions if applicable

{SYSTEM_PROMPT_STRUCTURE_GUIDE}

## Few-Shot Examples

{get_profile_examples_text()}

## User's Description
{description}
{clarification_section}{reference_section}## Output Format

Analyze the user's description and generate a complete profile configuration.

Output ONLY a valid JSON object (no markdown code blocks, no explanation):

{{
  "name": "Profile Name",
  "description": "What this profile is for",
  "system_prompt_content": null,
  "allowed_tools": null,
  "disallowed_tools": null,
  "enabled_agents": null,
  "memory": null,
  "include_partial_messages": true,
  "app_config_tools": false,
  "env": null,
  "ai_tools": null
}}

Notes:
- Use null for fields that should use defaults
- Output ONLY the fields shown above - no other fields exist
- For ai_tools, only include if creative features are needed:
  {{"image_generation": true, "video_generation": true}}
- Be creative with the name and description
- Match the model to the task complexity
- Restrict tools when appropriate for security/focus

Generate the profile configuration now:'''


# ============================================================================
# Helper Functions
# ============================================================================

def _format_domain_templates() -> str:
    """Format domain templates for inclusion in prompt."""
    lines = []
    for template_id, template in DOMAIN_TEMPLATES.items():
        lines.append(f"- **{template_id}**: {template.get('domain', 'general')} domain, "
                    f"model={template.get('model', 'sonnet')}, "
                    f"tools={template.get('tools', 'all')}")
    return "\n".join(lines)
