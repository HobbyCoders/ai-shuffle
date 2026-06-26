<!--
title: Gmail
description: Send and receive emails through Claude via the Gmail API
order: 13
-->

# Gmail Connector

## Overview

The Gmail connector integrates AI Shuffle with your Gmail account, allowing Claude to read incoming emails and send replies on your behalf. The connector polls your mailbox for new unread messages and surfaces them as incoming events; Claude can then send new emails or threaded replies.

The connector uses the official [Gmail API](https://developers.google.com/gmail/api) with OAuth 2.0 credentials. You supply a pre-obtained OAuth client ID, client secret, and refresh token; the connector uses these to mint short-lived access tokens directly against Google's token endpoint. There is no interactive browser authorization step inside AI Shuffle -- you obtain the refresh token yourself ahead of time and provide it as config.

## Prerequisites

- A **Google account** with Gmail enabled
- Access to the [Google Cloud Console](https://console.cloud.google.com/)
- A **Google Cloud project** with the Gmail API enabled
- **OAuth 2.0 client credentials** (Desktop or Web application type)
- Network access from AI Shuffle to Google's API servers (`googleapis.com`)

## Setup Guide

### 1. Create a Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Click the project dropdown at the top of the page and select **New Project**
3. Enter a project name (e.g., `AI Shuffle Gmail`) and click **Create**
4. Wait for the project to be created, then select it from the project dropdown

### 2. Enable the Gmail API

1. In your Google Cloud project, go to **APIs & Services > Library** (or navigate to [https://console.cloud.google.com/apis/library](https://console.cloud.google.com/apis/library))
2. Search for **Gmail API**
3. Click on **Gmail API** in the results
4. Click **Enable**

### 3. Configure the OAuth consent screen

Before creating credentials, you need to configure the OAuth consent screen:

1. Go to **APIs & Services > OAuth consent screen**
2. Select **External** as the user type (unless you have a Google Workspace organization), then click **Create**
3. Fill in the required fields:
   - **App name:** `AI Shuffle`
   - **User support email:** Your email address
   - **Developer contact information:** Your email address
4. Click **Save and Continue**
5. On the **Scopes** page, click **Add or Remove Scopes** and add:
   - `https://www.googleapis.com/auth/gmail.readonly` -- Read email messages and settings
   - `https://www.googleapis.com/auth/gmail.send` -- Send emails on your behalf
   - `https://www.googleapis.com/auth/gmail.modify` -- Read, send, delete, and manage your email
   - `https://www.googleapis.com/auth/gmail.compose` -- Manage drafts and send emails
6. Click **Update**, then **Save and Continue**
7. On the **Test users** page, click **Add Users** and add your Gmail address. This is required while the app is in "Testing" mode.
8. Click **Save and Continue**, then **Back to Dashboard**

**Note:** While in "Testing" mode, only test users you explicitly add can authorize the app. This is fine for personal use. Publishing the app for production use requires Google's review process.

### 4. Create OAuth 2.0 credentials

1. Go to **APIs & Services > Credentials**
2. Click **Create Credentials > OAuth client ID**
3. Select **Desktop app** as the application type (recommended for local AI Shuffle instances)
4. Enter a name (e.g., `AI Shuffle Connector`)
5. Click **Create**
6. A dialog will show your Client ID and Client Secret. Click **Download JSON** to save the credentials file.
7. The downloaded file will be named something like `client_secret_XXXX.apps.googleusercontent.com.json`

### 5. Obtain a refresh token

AI Shuffle does not run an interactive OAuth flow. You must obtain a long-lived **refresh token** yourself and supply it (along with the client ID and secret) as connector config. A common way to do this is the [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/):

1. Open the OAuth Playground, click the gear icon, check **Use your own OAuth credentials**, and paste your Client ID and Client Secret.
2. In the left panel, authorize the Gmail scopes you need (for example `https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/gmail.send`, or `https://www.googleapis.com/auth/gmail.modify`).
3. Click **Authorize APIs**, sign in with the target Gmail account, and grant access.
4. Click **Exchange authorization code for tokens** and copy the **Refresh token** value.

Make sure your OAuth client's authorized redirect URIs include the Playground URI (`https://developers.google.com/oauthplayground`) if you use this method.

### 6. Create the Gmail connector in AI Shuffle

Provide `client_id`, `client_secret`, and `refresh_token`. Optionally set `labels`, `max_results`, and `poll_interval_seconds`.

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Gmail",
    "connector_type": "gmail",
    "config": {
      "client_id": "123456789-abcdefg.apps.googleusercontent.com",
      "client_secret": "GOCSPX-AbCdEfGhIjKlMn",
      "refresh_token": "1//0gABCdEfGhIjKlMnOpQrStUvWxYz",
      "labels": "INBOX",
      "max_results": "10",
      "poll_interval_seconds": "60"
    }
  }'
```

Or use the AI Shuffle UI: **Settings > Connectors > Add Connector > Gmail**.

### 7. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

On activation, the connector exchanges your refresh token for an access token and starts a background poll loop that checks for new unread messages every `poll_interval_seconds`.

### 8. Test the connector

Try these in an AI Shuffle chat:

- "Send an email to alice@example.com with the subject 'Meeting Tomorrow' and let her know the meeting is at 3 PM"
- Send a new email to an address you control, then watch the connector pick it up as a new incoming message on its next poll.
- Reply to one of the incoming emails; the connector sends it as a threaded reply to the original message.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `client_id` | string | Yes | Google OAuth 2.0 client ID from the Google Cloud Console. |
| `client_secret` | string | Yes | Google OAuth 2.0 client secret. |
| `refresh_token` | string | Yes | OAuth 2.0 refresh token obtained from the authorization flow. Used to mint access tokens. |
| `labels` | string | No | Comma-separated Gmail label IDs to poll. Defaults to `INBOX`. |
| `max_results` | string | No | Maximum number of messages to fetch per poll. Defaults to `10`. |
| `poll_interval_seconds` | string | No | Seconds between inbox polls. Defaults to `60`. |

## Capabilities

The Gmail connector reports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `read_receipts` | Yes | The connector tracks which messages it has already seen. |
| `file_upload` | Yes | File upload is advertised as a capability. |
| `is_messaging` | No | Gmail is treated as a non-messaging (email) connector rather than a chat platform. |
| `rich_text` | No | Outgoing emails are sent as plain text (`text/plain`). |
| `typing_indicator` | No | Not applicable for email. |
| `reactions` | No | Not applicable for email. |
| `threads` | No | Not advertised as a connector capability (though replies preserve the Gmail thread). |
| `buttons` | No | Not supported. |

### How inbound email works

The connector does not subscribe to push notifications. Instead, after activation it runs a background loop that **polls Gmail for unread messages** every `poll_interval_seconds` (default 60). On each poll it lists up to `max_results` unread messages across the configured `labels` (default `INBOX`), and any message it has not seen before is surfaced as a new incoming message and as an `email_received` trigger.

### What Claude can do with Gmail

- **Receive emails** -- New unread messages in the watched labels are picked up on each poll and delivered to Claude with the sender, subject, snippet, body, and labels.
- **Send email** -- Compose and send a new plain-text email with a `to`, `subject`, and `body`.
- **Reply in-thread** -- Reply to a received message; the connector sets the proper threading headers so the reply stays in the original Gmail conversation.

## Troubleshooting

### "Access blocked: This app's request is invalid" while obtaining the token

- **Redirect URI mismatch.** When obtaining the refresh token (for example via the OAuth Playground), ensure the redirect URI used matches an authorized redirect URI on your OAuth client. For the OAuth Playground, add `https://developers.google.com/oauthplayground`.
- **Wrong credential type.** Make sure the OAuth client type matches how you are obtaining the token.

### "Google hasn't verified this app" warning

This is expected for apps in Testing mode while you authorize and obtain a refresh token. Click **Advanced**, then continue to the app. This warning only appears during the authorization step you perform to get the refresh token. For production use, you would need to submit the app for Google's verification, but this is not necessary for personal use.

### "Error 403: access_denied" during authorization

- **Test user not added.** While the app is in Testing mode, only users explicitly added as test users can authorize. Go to **Google Cloud Console > APIs & Services > OAuth consent screen > Test users** and add your Gmail address.
- **Wrong account.** Ensure you are signing in with the Gmail account that was added as a test user.

### Token refresh fails or "invalid_grant" error

The connector mints access tokens from the `refresh_token` you provided. If that fails:

- **Token revoked or expired.** If you revoked access in your Google account settings (**myaccount.google.com > Security > Third-party apps with account access**), or the refresh token expired (refresh tokens for apps in Testing mode expire after about 7 days), obtain a new refresh token and update the `refresh_token` config value.
- **Credentials regenerated.** If you regenerated the client secret in Google Cloud Console, the existing refresh token is invalidated. Update `client_secret` (and obtain a fresh `refresh_token`).
- **Clock skew.** OAuth token validation is time-sensitive. Ensure the system clock on your AI Shuffle host is accurate (within a few seconds of actual time).

### Emails send but with wrong "From" address

- The Gmail API sends emails from the authenticated account. You cannot spoof the sender address. If you need to send from a different address, configure a "Send as" alias in Gmail settings and specify it in the email composition.

### Rate limiting or quota errors

The Gmail API has usage quotas:

- **250 quota units per user per second** (most operations cost 5-50 units)
- **Daily sending limit:** 500 emails/day for regular Gmail, 2,000/day for Google Workspace

If Claude is making many rapid requests, you may hit these limits. Space out operations or use batch requests where possible. Check your quota usage in the [Google Cloud Console > APIs & Services > Gmail API > Quotas](https://console.cloud.google.com/apis/api/gmail.googleapis.com/quotas).
