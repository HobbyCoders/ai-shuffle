# Worker reachability on Android

The Workers screen shows a small indicator and text in each existing row. Online means that worker responded to this phone. Offline means **not reachable from this phone**, not that the computer is powered off. Registered account membership never implies that a worker is online.

Account workers are checked at their saved HTTPS origin's `/api/v1/mobile/hub/endpoint`. A 200 response must have Boolean `ready: true`, Boolean `protected_ingress: true`, and the exact saved `hubServerId` as `server_id`. That ID identifies the worker; the mobile enrollment's `deviceId` identifies this phone. Direct workers use `/api/v1/health`, requiring `status: healthy`, nonempty service/version strings and a Boolean authenticated field.

Checks send no account token, worker API key, WebView cookie or session mint request. Only explicitly configured Cloudflare Access service headers can accompany a request to its original saved HTTPS origin. Redirects, cookie storage and HTTP caching are disabled. Responses are bounded to 8 KiB and calls to six seconds, with at most three concurrent checks.

Two consecutive connection refusals/timeouts or gateway-unreachable 502/504/521–524/530 responses on the same validated network are required for Offline. The first such failure is Unknown. DNS, TLS, authorization, malformed responses, wrong worker identity and other service errors show Status unavailable. Missing Android network validation can never establish Offline; a successful LAN probe still establishes Online. Network changes clear previous evidence.

The screen checks on resume, manual Refresh and about every 30 seconds while visible. Observations expire after 90 seconds; nothing is persisted. Leaving the screen cancels requests and clears observations, so a changed network cannot inherit green on resume. Account, origin, worker identity or saved credential changes discard late results. Normal status updates change only the row's label and dot, preserving scrolling, open options and row taps.

Unit tests cover the strict wire contract, credential boundaries, failure classification, confirmation rules and expiry. Synthetic instrumentation exercises distinct account worker IDs, manual direct-worker Online → Unknown → Offline → Online, accessible explanations, preserved row/options controls, cancellation, changed saved identity and clearing old-network green on resume. Test transport and network snapshots are isolated from real workers; no worker is stopped to test Offline.
