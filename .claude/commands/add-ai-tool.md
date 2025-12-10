# Add AI Tool

You are helping the user design and implement a new AI tool for the AI Hub system.

## User Request
The user wants to add a new AI tool with the following details:
- **Feature Description**: $ARGUMENTS

## Your Task

### Step 1: Gather Requirements
Ask clarifying questions about:
1. **Provider**: Which AI service will power this? (e.g., Google, OpenAI, ElevenLabs, Stability AI, etc.)
2. **API Details**: Do you have the API documentation URL? What authentication method does it use?
3. **Capabilities**: What specific operations should this tool support? (e.g., generate, edit, transform)
4. **Input/Output**: What does the user provide? What does the tool produce? (file types, formats)
5. **Pricing**: What is the pricing model? (per request, per second, per token, etc.)

### Step 2: Design the Tool
Once you have the requirements, design the implementation:

1. **Provider File** (`/opt/ai-tools/providers/{category}/{provider-name}.ts`):
   - Implement the provider interface (`ImageProvider`, `VideoProvider`, or create a new one)
   - Define models with capabilities, pricing, and constraints
   - Implement API calls with proper error handling
   - Add file saving logic for generated content

2. **Tool Wrapper** (`/opt/ai-tools/{category}/{tool-name}.ts`):
   - Define input/output interfaces with JSDoc documentation
   - Handle environment variable resolution
   - Delegate to provider and format responses

3. **Registry Update** (`/opt/ai-tools/providers/registry.ts`):
   - Import and register the new provider

4. **Backend Updates** (if needed):
   - Settings API endpoints for configuration
   - File serving endpoints for new content types
   - Database settings storage

5. **UI Updates** (if needed):
   - Settings page integration configuration
   - Profile management AI tools list

### Step 3: Implementation Plan
Create a detailed implementation plan with:
- File locations and changes
- Type definitions
- API integration details
- Environment variables needed
- Testing approach

### Step 4: Implement
After user approval, implement the changes:
1. Create/update provider files
2. Update registry
3. Update types if needed
4. Add backend endpoints if needed
5. Rebuild TypeScript: `cd /opt/ai-tools && npm run build`

## Reference Architecture

Current AI tools location: `/opt/ai-tools/`

```
/opt/ai-tools/
├── providers/
│   ├── types.ts           # Provider interfaces
│   ├── registry.ts        # Central registry
│   ├── image/             # Image providers (google-gemini, openai-gpt-image)
│   └── video/             # Video providers (google-veo, openai-sora)
├── image-generation/      # Image tool wrappers
├── video-generation/      # Video tool wrappers
└── index.ts               # Main exports
```

## Current Providers

**Image Providers:**
- `google-gemini`: Nano Banana (text-to-image, image-edit, image-reference)
- `openai-gpt-image`: GPT Image 1 (text-to-image, image-edit)

**Video Providers:**
- `google-veo`: Veo 3.1 (text-to-video, image-to-video, video-extend, frame-bridge)
- `openai-sora`: Sora 2 (text-to-video, image-to-video)

## Example: Adding a New Provider

```typescript
// /opt/ai-tools/providers/audio/elevenlabs.ts
import { AudioProvider, AudioResult, UnifiedAudioInput, ProviderCredentials } from '../types.js';

export const elevenlabsProvider: AudioProvider = {
  id: 'elevenlabs',
  name: 'ElevenLabs',
  models: [
    {
      id: 'eleven_turbo_v2_5',
      name: 'Turbo v2.5',
      description: 'Fast, high-quality speech synthesis',
      capabilities: ['text-to-speech'],
      pricing: { unit: 'character', price: 0.00003 }
    }
  ],

  async generate(input, credentials, model): Promise<AudioResult> {
    // Implementation...
  },

  async validateCredentials(credentials) {
    // Validate API key...
  }
};
```

Now, tell me about the AI tool you want to add!
