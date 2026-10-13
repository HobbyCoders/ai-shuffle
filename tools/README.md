# AI Shuffle tools

These TypeScript modules implement the app's media-generation tools through fal.ai.
Configure a fal.ai API key in the app's provider settings. Enabling and calling a
generation tool can incur provider charges; the build and unit checks make no model calls.

## Build and runtime

Use Node.js 22.12+ (20.19+ also satisfies the current build tools):

```sh
npm ci --ignore-scripts
npm run build
```

The build removes `dist/` before compiling, so deleted modules cannot remain in
production. Generated JavaScript is not committed. Native `run_local.py --setup`
and `--update`, Docker, and release setup build these sources. `worker.ts` is the
active stdin/stdout JSON-RPC entry point and the source of the callable tool list.

## Current callable tools

| Tool | Purpose |
|---|---|
| generateImage | Generate an image |
| editImage | Edit an existing image |
| generateWithReference | Generate using reference images |
| generateVideo | Generate a video |
| imageToVideo | Animate a reference image |
| extendVideo | Extend a video |
| textTo3D, imageTo3D | Generate 3D assets |
| generateAudio, audioToAudio | Generate or transform audio |
| runFalModel | Call a configured fal.ai model |

See each function's TypeScript input interface for supported arguments. Provider
model capabilities and pricing can change; use the app's model discovery and the
provider's current pricing before a paid call. Results include generated-file URLs
for display in chat; do not expand image files into base64 text. Files are saved in
the active project working directory's generated-media folders.

The old Google/Meshy integration-card instructions and retexture3D, rig3D,
animate3D, getTask3D, textToSpeech and speechToText tool examples no longer describe
this tool surface. Voice input/read-aloud are separate app features.

## Scripts

`scripts/verify-network-vectors.mjs` checks the worker network's crypto test
vectors (`tests/core/network/vectors.json`) with Node's WebCrypto, the API the
account hub and the mobile app use, so the Python primitives in
`app/network/crypto.py` stay verifiable outside Python. Run it from the
repository root with `node tools/scripts/verify-network-vectors.mjs`; CI runs it
in the JavaScript job.
