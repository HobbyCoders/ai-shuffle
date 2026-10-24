# Native generation: images and video

## Guarded compatibility aliases (#545)

The registered `GenerateNativeImage` and `GenerateNativeVideo` names now enter
`Resolver.generate` with the same trusted current-turn context and `Sources`
backend as `generateImage` and `generateVideo`. Existing native grants and
capability-based discovery remain intact; raw native handlers are transport
helpers, not registered tool entry points. These native grants do not authorize
fal generation, so the resolver excludes that source from their choices. The bound chat provider no longer
implicitly selects a generation account. A remembered/job choice or an explicit
interactive choice identifies the account/model/worker and its validated options.

With no saved choice, interactive calls return the unified `choose` envelope and
unattended calls skip. Unreadable choices, unavailable options, unknown prices,
above-threshold confirmation and daily-cap exhaustion use the unified admission
rules. Subscription images carry explicit Included pricing and still reserve
against the daily cap; the verified SuperGrok video estimate remains $0.02, not
free or unlimited. Approved retries accept `source`, `model`, `worker`, `options`,
`confirmed` and the one-use `confirmation_token`. Legacy image fields
`image_model`, `size`, `quality` and `format` adapt to the unified request;
conflicting legacy/unified fields refuse instead of silently overriding a pick.

The adapter submits only after admission and preserves unified structured result
fields. Frontend alias choice/confirmation-card recognition is tracked separately;
JSON parity does not establish rendered-card support (#550). The sections below describe
the raw native transports and earlier verification history, not a bypass around
these registered-tool admission rules. Live no-paid-submission follow-up:
`harness-basics.native-generation-admission` in the live-smoke catalog.

## Native transports and earlier verification

`GenerateNativeImage` is exposed only on the Responses transport with a bound
account whose Image capability is available. OpenAI/xAI API-key and no-account
(including environment-key fallback) sessions lose the tool until verified.
Unbound direct calls are refused without sending a request. It uses the bound
provider row and credential, not chat-model name sniffing. ChatGPT uses a separate request containing
the native `image_generation` tool and matching `tool_choice`. SuperGrok
subscription sessions instead use the tested xAI `/v1/images/generations`
endpoint with `grok-imagine-image`, `n=1`, square `1k` and base64 output. No
OpenAI-specific fields reach xAI and neither route falls back to another provider.
The assistant-facing function participates in the ordinary tool permissions and
allow/deny lists.

This route does not require fal.ai. A failure in either route says nothing about
the other route. The provider settings panel explains this separation. Provider
or model naming alone does not establish access: the actual configured endpoint
validates the requested combination on use. An unsuccessful request reports the
HTTP status and a safe provider error code, without echoing response bodies that
could contain credentials. Unknown account capabilities are not advertised as
verified support.

The optional `image_model` selects the exact provider-native image model; omitting
it reads the session account's Native generation Image group. Overrides
are validated against the verified capability table, currently only ChatGPT's
provider-selected model/auto size/auto quality/PNG and SuperGrok's
`grok-imagine-image`/square 1k/JPEG. SuperGrok quality is hidden/null until tested.
OpenAI/xAI API-key accounts appear with locked, not-yet-verified options rather
than inheriting a subscription's support. Native generation is account-scoped
and does not change the AI Shuffle direct tools or their existing ChatGPT choice.
Stale account/legacy settings return `stale: true` with a sanitized GET reason.
When JSON parses, groups are validated independently: explicit null remains
disabled, valid groups are preserved, invalid Image uses its verified default
(or null if unavailable), and invalid Video becomes null. Unparseable JSON
uses the verified Image default (or null if unavailable) and disables Video
because the owner's choice is unknown. Accounts with no stored settings retain
their provider defaults, including Video on SuperGrok. Tools use the same
recovery without rewriting the stored row; one stale account cannot break the
list. The settings UI is cancelled and superseded by #379 ("say what you want"
generation). The unscoped direct `supergrok-subscription` model lane is not
included. See the mirrored settings API reference for the GET/PUT schema. OpenAI's
[image-tool guide](https://developers.openai.com/api/docs/guides/tools-image-generation)
documents the native tool, forced tool choice, final base64 output, and examples
using GPT-6 Astra with GPT Image 2.5 Sunburst. These examples do not establish
support for another account, subscription endpoint or compatible provider.

The stream normalizer separates progress/partial previews from final image
results and deduplicates images repeated in the terminal response. The tool
requests final PNG output without partial previews on ChatGPT. SuperGrok's
tested output is JPEG. Final images have bounded size and PNG/JPEG container
validation, and MIME type/extension follow the returned bytes. Workspace sessions save them under
`generated-images` and return a session-scoped download URL. Chat sessions store
session-owned artifacts and never accept a filesystem destination. Both routes
return inline image blocks through the existing durable tool-result pipeline,
so reopening a session preserves the rendered image.

Cancellation closes the provider stream. A cancelled workspace write is drained
and its newly created files removed; shared content-addressed artifacts are
never deleted as cancellation cleanup. Incomplete preview images are not saved
as final output. Ambiguous transport/server failures do not automatically replay
a native image request, since it may already have produced a billed image. The
explicit 401 credential-refresh path shares one refresh budget across a media
operation. Only a definite 401 retries its rejected request once with the new
credential, including polls; accepted jobs and ambiguous failures are never
resubmitted.

Provider-observed usage is recorded once for the auxiliary request, including
observations received before cancellation. Image charges are not estimated from
the orchestrator's text-token price. Usage marks this cost as incomplete, and
the tool/settings explain that session cost estimates exclude native image
charges.

## SuperGrok video

`GenerateNativeVideo` is offered only on SuperGrok subscription sessions and
uses the same server-side credential resolver. It sends one
`/v1/videos/generations` submission, then polls `/v1/videos/{request_id}` at
5-second intervals with a 180-second total deadline. Timeout/cancellation never
resubmits a job that may still finish and incur charges. Successful MP4 output
is downloaded from an allowlisted HTTPS media host without subscription headers
or URL logging and saved as a session-owned artifact. The playable
`/api/v1/artifacts/{hash}/video.mp4?session_id=...` alias delegates the existing
artifact/session authorization, serves inline MP4 with byte-range support, and
keeps signed holder redirects on the playable route. Timeout text explicitly
requires the user's go-ahead before calling again. Provider-reported cost stays
in the JSON result and structured result fields; inline ToolUseMessage playback
and visible cost presentation remain pending in the separate frontend build.

Only `grok-imagine-video-1.5-lite`, 1 s, 480p, 16:9, silent text-to-video is
selectable. Documented but untested durations/resolutions/aspects/audio are
locked. No ChatGPT video controls, API-key video routes, image-to-video or
editing are offered. The tool returns provider-reported USD ticks and decimal
cost separately from chat token pricing; this is not free/unlimited usage.

The coordinated 2026-10-21 live probe submitted this verified combination once,
polled the same job four times, and downloaded an 848×480 H.264 MP4, 83,831 bytes,
24 fps, 25 decoded frames and no audio. The job completed in 23.643 seconds and
reported 200,000,000 USD ticks ($0.02). That probe verified the real provider's
producing path, not this branch's final tool wiring, running-app UI or billing
invoice/quota debit. The branch's video integration is covered by synthetic HTTP
boundary tests; it made no additional billed video request.

## Acceptance

On 2026-10-21, two isolated Linux handler probes used the connected SuperGrok
subscription through the production credential resolver (read-only credential
snapshots), settings loader, xAI request path and workspace image saver. Both
returned HTTP 200, `grok-imagine-image`, square `1k`, one 1024×1024 JPEG each.
The initial probe returned 130,034 bytes in 5.874 seconds. The final
account-scoped settings probe returned 131,108 bytes in 5.345 seconds. Both saved
images decoded successfully and auxiliary image usage was recorded. Total:
two image requests, no retries and no native Responses-tool check. These were
real-provider handler checks, not running-app/UI or permission-card checks.
No request for `grok-imagine-image-2.0` or `2k` was made; both remain locked.
ChatGPT's default request shape is unchanged and was not re-run live.

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
