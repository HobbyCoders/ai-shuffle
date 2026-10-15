<!--
title: Microsoft Teams
description: Experimental Microsoft Teams connector via Azure Bot
order: 7
-->

# Microsoft Teams (experimental)

**Hidden by default.** Teams remains `offered: false` until a real Azure Bot registration has passed the live checks below. The implementation has local authentication, routing and reply tests, not a verified Azure deployment. This is for administrators willing to test an experimental integration, not a promise of production support.

## Opt in

Set `AI_SHUFFLE_EXPERIMENTAL_MSTEAMS=1` in the environment of the worker that will host the bot, then restart that worker. This enables Teams in **Settings → Plugins → Apps** and permits installation and account creation. Install (or update) the Teams plugin before connecting it. The flag must remain set to receive activities. Removing it and restarting hides Teams and makes its messaging endpoint return 404; saved accounts are not deleted. Deactivate the account to stop all connector activity.

## Azure and Teams setup

1. In Microsoft Entra ID, create an app registration for your organization and record the **Application (client) ID** and **Directory (tenant) ID**. Create a client secret and copy its **value**, not its secret ID. Enter secrets only in the app's credential fields, never in chat or in a URL.
2. Create an **Azure Bot** resource using that app registration, configured as **Single Tenant**, with the same tenant ID. Enable its **Microsoft Teams** channel. This connector uses client credentials; managed identity and government-cloud endpoints are not supported. Existing multi-tenant bots can leave the connector's tenant field blank (`botframework.com`); use the tenant UUID for single-tenant bots.
3. In AI Shuffle, create a Microsoft Teams account with the app ID, client secret and tenant ID. Set **Allowed User IDs** to the permitted users' Microsoft Entra **object IDs** (not email addresses). An empty sender list is not supported. Choose the account's agent/routing settings and activate it. The credential test only checks OAuth; it does not prove message delivery.
4. Obtain the saved connector's `id` from its account API response (`GET /api/v1/connectors`, authenticated as the owner). Set the Azure Bot **Messaging endpoint** to:

   ```text
   https://YOUR-PUBLIC-HOST/api/v1/connectors/msteams/CONNECTOR-ID/messages
   ```

   Route this exact HTTPS path through your reverse proxy to the worker running this connector. Preserve the `Authorization` header and JSON body. Do not put a browser sign-in page or an owner-session requirement in front of this path: it authenticates Microsoft channel JWTs itself. Do **not** expose the worker's administration API just to expose this endpoint. Requests are limited to 256 KB.
5. In Teams Developer Portal, create a Teams app whose bot uses the same application ID. Enable the desired personal, team or group-chat scopes, install it in your approved test tenant, and start with a personal conversation. Team/channel messages may require an `@mention` according to Teams policy.
6. Send a message from an allowed user. The authenticated activity supplies the conversation reference; no manual conversation ID or service URL is needed. AI Shuffle strips the bot mention and delivers the message through normal connector routing. Replies use the authenticated reference. References persist in the hosting worker's data directory, scoped to the connector, app ID and tenant; changing app or tenant does not reuse the old references. Moving the account to another worker requires a fresh inbound message there.

## Security and delivery

Only RS256 channel tokens from the public Bot Framework OpenID/JWKS trust source are accepted. The receiver checks issuer, bot audience, expiration/not-before (60-second clock skew), the `msteams` signing-key endorsement and channel ID, the token's `serviceurl` against the activity, and the configured single-tenant ID. Keys are cached for one hour and refreshed for a new key ID, with a short refresh rate limit. Emulator and skill tokens are not accepted.

Allowed-user checks run before messages, card responses or conversation references reach the runtime. References are saved only from authenticated, authorized messages/invokes. Outbound bot tokens are acquired with client credentials, cached and refreshed before expiration. Replies and actions retry HTTP 429 and 5xx responses up to four total attempts (bounded backoff), and retry once with a fresh token after a 401. Retries after server errors can duplicate a message if the server accepted it before returning the error. Persistent failures are reported as failed connector actions; they are not treated as successful delivery.

Outbound service URLs must use HTTPS on `smba.trafficmanager.net` or `smba.infra.teams.microsoft.com`, with no credentials, query, fragment or nonstandard port. Redirects are not followed. Other sovereign clouds or endpoint hosts need separate reviewed support rather than an override.

## Required live verification before general availability

An administrator with an approved Azure Bot registration and test tenant must verify all of these; local tests are not a substitute:

- With the opt-in set, install/update the runtime and create/activate the account through Apps; without it, Teams stays hidden and cannot be installed or receive activities.
- Azure reaches the public messaging endpoint through the real proxy, and a genuine Teams channel JWT passes signature, audience, endorsement, tenant and service URL checks. Missing/forged tokens, other bot audiences/tenants and unlisted users do not create a session or reference.
- An allowed user sends a personal message (then each enabled group/team scope); the correct agent/session receives the normalized message and its reply arrives in the **same** Teams conversation/thread.
- Restart the hosting worker and confirm a reply can use the persisted reference. Send another message and verify routing still works; a different app/tenant must not reuse that state.
- Check real single-tenant client-credentials acquisition, token renewal and secret rotation. Confirm 401, throttling and service errors are visible and do not produce false success.
- Exercise approval/denial cards with an allowed user and ensure a disallowed user cannot approve. Review Teams app permissions and tenant policy before expanding access.

Do not change the shipped `offered: false` default until these checks have passed and their results have been reviewed.
