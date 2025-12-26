# Meshy AI Integration Plan

## Overview

This document outlines the comprehensive plan to integrate Meshy AI as a 3D model generation provider into the AI Hub application. The integration includes:

1. **Backend Provider Integration** - Add Meshy as a new provider type
2. **AI Tools Creation** - Create tools for all Meshy API features
3. **Settings Configuration** - Add API key management for Meshy
4. **Profile Configuration** - Update profiles to show new AI tools
5. **Studio UI Redesign** - Add 3D model generation workspace

---

## 1. Meshy AI API Reference

### Base URL
`https://api.meshy.ai`

### Authentication
- Header: `Authorization: Bearer {api_key}`

### Available Endpoints

#### Text to 3D
- **Create Preview**: `POST /openapi/v2/text-to-3d` (20 credits for Meshy-6, 5 credits for others)
- **Create Refine**: `POST /openapi/v2/text-to-3d` (10 credits with texture guidance)
- **Get Task**: `GET /openapi/v2/text-to-3d/:id`
- **List Tasks**: `GET /openapi/v2/text-to-3d`
- **Delete Task**: `DELETE /openapi/v2/text-to-3d/:id`
- **Stream Task**: `GET /openapi/v2/text-to-3d/:id/stream` (SSE)

**Parameters:**
- `prompt` (required, max 600 chars)
- `mode`: "preview" | "refine"
- `art_style`: "realistic" | "sculpture"
- `ai_model`: "meshy-4" | "meshy-5" | "meshy-6" | "latest"
- `topology`: "quad" | "triangle"
- `target_polycount`: 100-300,000

#### Image to 3D
- **Create**: `POST /openapi/v1/image-to-3d` (20 credits for Meshy-6, 5 for others + 10 for texturing)
- **Get Task**: `GET /openapi/v1/image-to-3d/:id`
- **List Tasks**: `GET /openapi/v1/image-to-3d`
- **Delete Task**: `DELETE /openapi/v1/image-to-3d/:id`
- **Stream Task**: `GET /openapi/v1/image-to-3d/:id/stream` (SSE)

**Parameters:**
- `image_url` (required) - Public URL or base64 data URI
- `ai_model`: "meshy-4" | "meshy-5" | "meshy-6" | "latest"
- `topology`: "quad" | "triangle"
- `target_polycount`: 100-300,000
- `symmetry_mode`: "off" | "auto" | "on"
- `should_remesh`: boolean
- `should_texture`: boolean
- `enable_pbr`: boolean
- `pose_mode`: "a-pose" | "t-pose" | ""
- `texture_prompt`: string (max 600 chars)

#### Retexture (AI Texturing)
- **Create**: `POST /openapi/v1/retexture`
- **Get Task**: `GET /openapi/v1/retexture/:id`
- **List Tasks**: `GET /openapi/v1/retexture`
- **Delete Task**: `DELETE /openapi/v1/retexture/:id`
- **Stream Task**: `GET /openapi/v1/retexture/:id/stream` (SSE)

**Parameters:**
- `input_task_id` OR `model_url` (required)
- `text_style_prompt` OR `image_style_url` (required)
- `ai_model`: "meshy-4" | "meshy-5" | "latest"
- `enable_original_uv`: boolean
- `enable_pbr`: boolean

#### Auto-Rigging
- **Create**: `POST /openapi/v1/rigging`
- **Get Task**: `GET /openapi/v1/rigging/:id`
- **Delete Task**: `DELETE /openapi/v1/rigging/:id`
- **Stream Task**: `GET /openapi/v1/rigging/:id/stream` (SSE)

**Parameters:**
- `input_task_id` OR `model_url` (required)
- `height_meters`: number (default 1.7m)
- `texture_image_url`: string (optional)

#### Animation
- **Create**: `POST /openapi/v1/animations`
- **Get Task**: `GET /openapi/v1/animations/:id`
- **Delete Task**: `DELETE /openapi/v1/animations/:id`
- **Stream Task**: `GET /openapi/v1/animations/:id/stream` (SSE)

**Parameters:**
- `rig_task_id` (required)
- `action_id` (required) - Animation identifier
- `operation_type`: "change_fps" | "fbx2usdz" | "extract_armature"
- `fps`: 24 | 25 | 30 | 60

### Output Formats
- GLB, FBX, OBJ, USDZ, STL, 3MF, BLEND, MTL

---

## 2. Backend Implementation

### 2.1 Type Definitions (`tools/providers/types.ts`)

Add new types for 3D model generation:

```typescript
// 3D Model Provider Types
export interface Unified3DInput {
  prompt: string;
  art_style?: 'realistic' | 'sculpture';
  ai_model?: 'meshy-4' | 'meshy-5' | 'meshy-6' | 'latest';
  topology?: 'quad' | 'triangle';
  target_polycount?: number;
  [key: string]: any;
}

export interface UnifiedImage3DInput extends Unified3DInput {
  image_path: string;
  symmetry_mode?: 'off' | 'auto' | 'on';
  should_remesh?: boolean;
  should_texture?: boolean;
  enable_pbr?: boolean;
  pose_mode?: 'a-pose' | 't-pose' | '';
}

export interface UnifiedRetextureInput {
  model_source: string; // task_id or model_url
  style_prompt?: string;
  style_image_url?: string;
  ai_model?: string;
  enable_original_uv?: boolean;
  enable_pbr?: boolean;
}

export interface UnifiedRiggingInput {
  model_source: string;
  height_meters?: number;
  texture_image_url?: string;
}

export interface UnifiedAnimationInput {
  rig_task_id: string;
  action_id: string;
  fps?: 24 | 25 | 30 | 60;
  operation_type?: 'change_fps' | 'fbx2usdz' | 'extract_armature';
}

export interface Model3DResult {
  success: boolean;
  task_id?: string;
  model_urls?: {
    glb?: string;
    fbx?: string;
    obj?: string;
    usdz?: string;
  };
  texture_urls?: {
    base_color?: string;
    metallic?: string;
    roughness?: string;
    normal?: string;
  };
  file_path?: string;
  progress?: number;
  status?: 'PENDING' | 'IN_PROGRESS' | 'SUCCEEDED' | 'FAILED' | 'CANCELED';
  error?: string;
}

export interface Model3DProvider {
  readonly id: string;
  readonly name: string;
  readonly models: ModelInfo[];

  textTo3D(input: Unified3DInput, credentials: ProviderCredentials, model: string): Promise<Model3DResult>;
  imageTo3D?(input: UnifiedImage3DInput, credentials: ProviderCredentials, model: string): Promise<Model3DResult>;
  retexture?(input: UnifiedRetextureInput, credentials: ProviderCredentials, model: string): Promise<Model3DResult>;
  rig?(input: UnifiedRiggingInput, credentials: ProviderCredentials): Promise<Model3DResult>;
  animate?(input: UnifiedAnimationInput, credentials: ProviderCredentials): Promise<Model3DResult>;
  getTask(taskId: string, endpoint: string, credentials: ProviderCredentials): Promise<Model3DResult>;
  validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }>;
}
```

### 2.2 Meshy Provider (`tools/providers/model3d/meshy.ts`)

Create new provider implementing all Meshy API endpoints:

```typescript
export const meshyProvider: Model3DProvider = {
  id: 'meshy',
  name: 'Meshy AI',
  models: [
    {
      id: 'meshy-6',
      name: 'Meshy 6 (Latest)',
      description: 'Latest Meshy model with best quality',
      capabilities: ['text-to-3d', 'image-to-3d', '3d-texturing']
    },
    {
      id: 'meshy-5',
      name: 'Meshy 5',
      description: 'Balanced quality and speed',
      capabilities: ['text-to-3d', 'image-to-3d', '3d-texturing']
    },
    {
      id: 'meshy-4',
      name: 'Meshy 4',
      description: 'Fast generation',
      capabilities: ['text-to-3d', 'image-to-3d', '3d-texturing']
    }
  ],
  // ... implementation
};
```

### 2.3 Registry Update (`tools/providers/registry.ts`)

Register the new 3D model provider:

```typescript
import { meshyProvider } from './model3d/meshy.js';

// Add to registry
private model3DProviders = new Map<string, Model3DProvider>();

registerModel3DProvider(provider: Model3DProvider): void { ... }
getModel3DProvider(id: string): Model3DProvider | undefined { ... }
listModel3DProviders(): Model3DProvider[] { ... }

// Auto-register
registry.registerModel3DProvider(meshyProvider);
```

---

## 3. AI Tools Implementation

Create new tools in `tools/model3d-generation/`:

### 3.1 textTo3D.ts
```typescript
export async function textTo3D(params: {
  prompt: string;
  art_style?: 'realistic' | 'sculpture';
  model?: string;
  topology?: 'quad' | 'triangle';
  target_polycount?: number;
}): Promise<Model3DResult>
```

### 3.2 imageTo3D.ts
```typescript
export async function imageTo3D(params: {
  image_path: string;
  prompt?: string;
  model?: string;
  should_texture?: boolean;
  enable_pbr?: boolean;
  symmetry_mode?: 'off' | 'auto' | 'on';
}): Promise<Model3DResult>
```

### 3.3 retexture3D.ts
```typescript
export async function retexture3D(params: {
  model_path_or_task_id: string;
  style_prompt?: string;
  style_image?: string;
  enable_pbr?: boolean;
}): Promise<Model3DResult>
```

### 3.4 rig3D.ts
```typescript
export async function rig3D(params: {
  model_path_or_task_id: string;
  height_meters?: number;
}): Promise<Model3DResult>
```

### 3.5 animate3D.ts
```typescript
export async function animate3D(params: {
  rig_task_id: string;
  action_id: string;
  fps?: number;
}): Promise<Model3DResult>
```

### 3.6 getTask3D.ts
```typescript
export async function getTask3D(params: {
  task_id: string;
  task_type: 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation';
}): Promise<Model3DResult>
```

---

## 4. Settings Card Updates

### 4.1 Add Meshy API Key Section

In `SettingsCard.svelte`, add to the API Keys section:

```svelte
<!-- Meshy API Key -->
<div class="setting-item">
  <label>Meshy API Key</label>
  <input
    type="password"
    bind:value={meshyApiKey}
    placeholder={meshyApiKeyMasked || 'Enter Meshy API key'}
  />
  <button onclick={saveMeshyKey}>Save</button>
</div>
```

### 4.2 Add 3D Model Selection

In the Models section:

```svelte
<!-- 3D Model Provider -->
<div class="model-section">
  <h4>3D Generation</h4>
  <select bind:value={selected3DModel}>
    {#each model3DGroups as [provider, models]}
      <optgroup label={provider}>
        {#each models as model}
          <option value={model.id}>{model.name}</option>
        {/each}
      </optgroup>
    {/each}
  </select>
</div>
```

---

## 5. Profile Card Updates

### 5.1 Add 3D AI Tools

Update `ProfileCard.svelte` to include new AI tool categories:

```typescript
// Add to AIToolCategory definitions
const model3DTools = [
  { id: 'text_to_3d', name: 'Text to 3D', description: 'Generate 3D models from text' },
  { id: 'image_to_3d', name: 'Image to 3D', description: 'Convert images to 3D models' },
  { id: 'retexture_3d', name: '3D Retexturing', description: 'Apply AI textures to 3D models' },
  { id: 'rig_3d', name: '3D Rigging', description: 'Auto-rig humanoid models' },
  { id: 'animate_3d', name: '3D Animation', description: 'Apply animations to rigged models' }
];
```

### 5.2 Update Form State

```typescript
// Add to profileForm
enabled_ai_tools: [
  // existing...
  'text_to_3d',
  'image_to_3d',
  'retexture_3d',
  'rig_3d',
  'animate_3d'
]
```

---

## 6. Studio UI Redesign

### 6.1 New Tab Structure

```
┌─────────────────────────────────────────────────────────────┐
│  [Image] [Video] [3D Models] [Voice] [Transcribe]           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────┐  ┌──────────────────────────┐  │
│  │                         │  │  Generation Controls     │  │
│  │     Preview Area        │  │                          │  │
│  │     (3D Viewer)         │  │  [Text to 3D]           │  │
│  │                         │  │  [Image to 3D]          │  │
│  │                         │  │  [Retexture]            │  │
│  │                         │  │  [Rig]                  │  │
│  │                         │  │  [Animate]              │  │
│  │                         │  │                          │  │
│  └─────────────────────────┘  └──────────────────────────┘  │
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  Generation History / Asset Library                     ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### 6.2 New Components

1. **Model3DGenerator.svelte** - Main 3D generation controls
2. **Model3DPreview.svelte** - 3D model viewer (using three.js/model-viewer)
3. **Model3DHistory.svelte** - 3D generation history
4. **AnimationLibrary.svelte** - Browse 500+ Meshy animations

### 6.3 Design System

**Color Scheme:**
- Primary: `hsl(var(--primary))` - Existing
- 3D Accent: `#7C3AED` (Purple for 3D features)
- Success: `#10B981`
- Warning: `#F59E0B`

**Layout Improvements:**
- Larger preview area (60% width on desktop)
- Collapsible control panels
- Floating action buttons for quick actions
- Progress indicators for long-running tasks

---

## 7. Implementation Order

### Phase 1: Backend (Priority: High)
1. Add 3D types to `types.ts`
2. Create `tools/providers/model3d/meshy.ts`
3. Update `registry.ts`
4. Create AI tools in `tools/model3d-generation/`

### Phase 2: Settings (Priority: High)
1. Add Meshy API key state
2. Add API key input UI
3. Add 3D model selection
4. Backend API endpoints for settings

### Phase 3: Profile Configuration (Priority: Medium)
1. Add new AI tool definitions
2. Update profile form state
3. Update save/load logic

### Phase 4: Studio UI (Priority: High)
1. Add 3D tab to StudioView
2. Create Model3DGenerator component
3. Create Model3DPreview component (3D viewer)
4. Update generation history for 3D models

### Phase 5: Testing & Polish (Priority: Medium)
1. Test all Meshy API endpoints
2. Error handling improvements
3. Loading states and progress
4. Mobile responsiveness

---

## 8. File Changes Summary

### New Files
```
tools/providers/model3d/
├── index.ts
└── meshy.ts

tools/model3d-generation/
├── textTo3D.ts
├── imageTo3D.ts
├── retexture3D.ts
├── rig3D.ts
├── animate3D.ts
└── getTask3D.ts

frontend/src/lib/components/deck/studio/
├── Model3DGenerator.svelte
├── Model3DPreview.svelte
└── AnimationLibrary.svelte

frontend/src/lib/types/
└── ai-models-3d.ts
```

### Modified Files
```
tools/providers/types.ts          - Add 3D types
tools/providers/registry.ts       - Register 3D provider
tools/index.ts                    - Export 3D tools

frontend/src/lib/components/deck/cards/SettingsCard.svelte  - Add Meshy settings
frontend/src/lib/components/deck/cards/ProfileCard.svelte   - Add 3D AI tools
frontend/src/lib/components/deck/studio/StudioView.svelte   - Add 3D tab
frontend/src/lib/stores/studio.ts                           - Add 3D state
frontend/src/lib/types/ai-models.ts                         - Add 3D models
```

---

## 9. Dependencies

### Frontend
- `@google/model-viewer` - For 3D model preview (WebGL-based)
- Or use existing three.js if available

### Backend
- No new dependencies (uses fetch for API calls)

---

## 10. API Endpoints (Backend)

### Settings
- `GET /api/settings/meshy` - Get Meshy config
- `PUT /api/settings/meshy` - Update Meshy config
- `POST /api/settings/meshy/validate` - Validate API key

### 3D Generation
- `POST /api/generate/3d/text` - Text to 3D
- `POST /api/generate/3d/image` - Image to 3D
- `POST /api/generate/3d/retexture` - Retexture
- `POST /api/generate/3d/rig` - Auto-rig
- `POST /api/generate/3d/animate` - Animate
- `GET /api/generate/3d/task/:id` - Get task status
- `GET /api/generate/3d/animations` - List available animations

---

## Notes

- All 3D generation is asynchronous (polling or SSE for status)
- Models are saved to `generated-models/` directory
- Support GLB as primary format (best web compatibility)
- Consider caching animation library (500+ options)
