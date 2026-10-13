# Native Responses image generation

`GenerateNativeImage` is exposed in sessions using the Responses transport. It
uses the session's current provider client, credential and orchestrator model.
The separate request contains the native `image_generation` tool and a matching
`tool_choice`; it never translates that tool into a function or falls back to
another provider. The assistant-facing function participates in the ordinary
tool permissions and allow/deny lists.

This route does not require fal.ai. A failure in either route says nothing about
the other route. The provider settings panel explains this separation. Provider
or model naming alone does not establish access: the actual configured endpoint
validates the requested combination on use. An unsuccessful request reports the
HTTP status and a safe provider error code, without echoing response bodies that
could contain credentials. Unknown account capabilities are not advertised as
verified support.

The optional `image_model` selects the exact provider-native image model; omitting
it uses that endpoint's default. `size` and `quality` are passed through after
shape validation, so the provider decides whether they are supported. OpenAI's
[image-tool guide](https://developers.openai.com/api/docs/guides/tools-image-generation)
documents the native tool, forced tool choice, final base64 output, and examples
using GPT-6 Astra with GPT Image 2.5 Sunburst. These examples do not establish
support for another account, subscription endpoint or compatible provider.

The stream normalizer separates progress/partial previews from final image
results and deduplicates images repeated in the terminal response. The tool
requests final PNG output without partial previews. Final images have bounded
size and PNG container validation. Workspace sessions save them under
`generated-images` and return a session-scoped download URL. Chat sessions store
session-owned artifacts and never accept a filesystem destination. Both routes
return inline image blocks through the existing durable tool-result pipeline,
so reopening a session preserves the rendered image.

Cancellation closes the provider stream. A cancelled workspace write is drained
and its newly created files removed; shared content-addressed artifacts are
never deleted as cancellation cleanup. Incomplete preview images are not saved
as final output. Ambiguous transport/server failures do not automatically replay
a native image request, since it may already have produced a billed image. The
existing explicit 401 credential-refresh retry remains available.

Provider-observed usage is recorded once for the auxiliary request, including
observations received before cancellation. Image charges are not estimated from
the orchestrator's text-token price. Usage marks this cost as incomplete, and
the tool/settings explain that session cost estimates exclude native image
charges.

## Acceptance

Regression coverage exercises the real Responses client, native loop, tool
dispatch, saved bytes, inline event serialization/replay, credential/endpoint
binding, denied/unsupported requests, no automatic retry, and cancellation with
synthetic HTTP responses. It does not establish live provider/account support.
Two loop-level cases drive the native loop through the real Responses client:
Stop while the native request is still generating (stream closed, one native
request, no saved image, no follow-up model call), and provider rejection with
a definitive (400) and an ambiguous (503) status (one native request, sanitized
error passed to the model, no fallback). These are synthetic regressions for
the outstanding live checks below, not substitutes for them.

On 2026-09-21, an isolated Windows worker from this branch produced a PNG using
the user's authorized ChatGPT subscription and `gpt-5.6-luna`, with fal.ai
unconfigured. Request metadata confirmed the same ChatGPT Responses endpoint and
Luna model, a native `image_generation` tool, forced native tool choice, and HTTP
200. The normal permission card gated the request. The 988,301-byte PNG was
stored as a session-owned artifact, displayed inline, and rendered again after
reopening the session. Its download returned HTTP 200 with `image/png` and a
SHA-256 matching the artifact address. The durable log recorded one auxiliary
usage event for the generated image with `cost_incomplete: true`.

The fresh worker also exposed a composer blocker: chat required an agent while
hiding its picker. The picker is now available in chat on desktop and mobile;
repository, worktree, runner and permission-mode controls remain hidden there.
The next live request was submitted through the corrected chat composer.

One additional, explicitly authorized cancellation test produced a second image.
The image result was saved at 21:33:22.026 Eastern; the Stop interrupt arrived at
21:33:23.682 and the turn aborted at 21:33:23.696. This confirms interruption of
the follow-up response, **not** cancellation during native generation. That live
case and live provider-rejection coverage remain outstanding for #1399; fixture
coverage must not be substituted for those observations.

Luna shortened the artifact hash in its first reply's written download link.
The deterministic tool image card retained the correct, verified download URL.
Do not infer that every model-written link is valid from a successful tool card.
Use the intended configured account for remaining acceptance; never substitute
a different credential or provider to make a check pass.

## Chats on other providers (#1796)

`GenerateNativeImage` is registered only for `openai_responses` sessions. When
Settings > Models names the ChatGPT subscription as the image model
(`image_model` / `image_edit_model` = `chatgpt-subscription`, or
`chatgpt-subscription/<openai image model>`), the ai-tools `generateImage`,
`editImage` and `generateWithReference` tools route to
`app/core/provider/subscription_images.py` instead of fal.ai, for every lane
(delegated Claude, native fluid on any model, voice). The handlers run in the
app process: they pick the newest usable ChatGPT row, resolve its sign-in
through `_build_provider_client` like a session would, and send one Responses
request with the `image_generation` tool, using that row's default text model.
The credential never reaches the session, the Node tool worker or the tool
result. A foreign model id is refused; no signed-in ChatGPT row yields a plain
message.

While this is selected, `runFalModel` refuses image work
(`_fal_image_refusal` in `app/mcp/ai_tools_sdk.py`):
- an explicit `output_type: image`;
- an endpoint the fal.ai catalog lists as an image model, whatever
  `output_type` says (the catalog wins);
- an endpoint absent from the catalog whose name marks an image model
  (`flux`, `imagen`, ...) and matches none of the Node worker's video, 3D or
  audio hints;
- an endpoint nothing can place, sent without `output_type`, because the Node
  worker's `inferOutputType` would run it as an image. The message asks for
  the model's output_type, so an unlisted music model costs one retry.

Known limit: an image model that is neither in the catalog nor recognizable by
name, sent with an explicit video, 3d or audio `output_type`, still runs. The
Python hint lists mirror `inferOutputType` in `tools/providers/fal-generic.ts`,
and a test keeps them matched.
These tools need no fal.ai key, so they stay offered when fal.ai has none.
Usage is not recorded on the calling session's cost (subscription plan).
