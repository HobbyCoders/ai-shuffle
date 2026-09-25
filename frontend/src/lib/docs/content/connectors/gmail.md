<!--
title: Gmail
description: Give your agent Gmail tools to send, search, read and organize email
order: 13
-->

# Gmail Connector

## Overview

The Gmail connector links your own Gmail account to AI Shuffle so your agent can send, search, read and organize email when you ask it to. It is not a chat bot: connecting never starts chats or sends automatic replies. If you turn on `watch_inbox`, the connector also checks for new mail and fires an `email_received` trigger that your automations can use.

The connector uses the official [Gmail API](https://developers.google.com/gmail/api) with OAuth 2.0. You connect an account with the **Connect with Google** button in AI Shuffle. The owner first sets up a Google OAuth app for the installation, once.

## Prerequisites

- A **Google account** with Gmail enabled
- Access to the [Google Cloud Console](https://console.cloud.google.com/)
- A **Google Cloud project** with the Gmail API enabled
- An **OAuth 2.0 client** of type **Web application**
- AI Shuffle reachable in your browser over HTTPS, or on `localhost`, so Google can send you back to it after sign-in
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
5. On the **Scopes** page, click **Add or Remove Scopes** and add the scope `https://www.googleapis.com/auth/gmail.modify` (read, compose, send and organize mail). This is the only scope AI Shuffle asks for, and it is required.
6. Click **Update**, then **Save and Continue**
7. On the **Test users** page, click **Add Users** and add your Gmail address. This is required while the app is in "Testing" mode.
8. Click **Save and Continue**, then **Back to Dashboard**

**Note:** While in "Testing" mode, only test users you explicitly add can authorize the app. This is fine for personal use. Publishing the app for production use requires Google's review process.

### 4. Open the Gmail app in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **Gmail**.
2. Click **Install Gmail** if prompted, then click **Connect account**.
3. Click **Google app setup (administrator)**. Note the **Authorized redirect URI** shown there. It has the form `https://<your-host>/api/v1/connectors/oauth/google/callback` (or a `localhost` address).

The redirect URI is built from the address you opened AI Shuffle with, and it must match that address when you save the settings and when you connect. Open AI Shuffle at the HTTPS or `localhost` address you want to use before you copy it.

### 5. Create the OAuth client

1. In Google Cloud, go to **APIs & Services > Credentials**
2. Click **Create Credentials > OAuth client ID**
3. Select **Web application** as the application type
4. Enter a name (e.g., `AI Shuffle Connector`)
5. Under **Authorized redirect URIs**, add the exact redirect URI from step 4
6. Click **Create** and copy the **Client ID** and **Client Secret**

### 6. Save the Google app settings

Back in **Google app setup (administrator)**, paste the **Google client ID** and **Google client secret**, then click **Save Google app settings**. This is a one-time setup for the installation. Accounts you connected earlier keep their credentials.

Instead of saving them in the UI, the host can set the `SHUFFLE_GOOGLE_CLIENT_ID`, `SHUFFLE_GOOGLE_CLIENT_SECRET` and `SHUFFLE_GOOGLE_REDIRECT_URI` environment variables. The UI then shows that the settings are managed by the host's environment.

### 7. Connect your Google account

1. Optionally change the connection name. Open **Advanced settings** to set `watch_inbox`, `labels` and the other options in the table below.
2. Click **Connect with Google**. A Google sign-in window opens. Allow pop-ups for AI Shuffle if it is blocked.
3. Sign in with the Gmail account and allow access.

When the window closes, the connection appears in the list. Turn on its **Activate** switch to start it.

To connect the same account again later (for example after revoking access), edit the connection and click **Reconnect with Google**. You must choose the same Google account; to add a different account, create a separate connection.

### 8. Test the connector

Try these in an AI Shuffle chat:

- "Send an email to alice@example.com with the subject 'Meeting Tomorrow' and let her know the meeting is at 3 PM"
- "Search my inbox for unread mail from the last 7 days and summarize it"

### Advanced: enter a refresh token manually

The connector still accepts a client ID, client secret and refresh token that you obtain yourself. Under **Advanced settings**, fill in **OAuth Client ID**, **OAuth Client Secret** and **OAuth Refresh Token**, then click **Create Connector**. One way to get a refresh token is the [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/):

1. Add `https://developers.google.com/oauthplayground` as an authorized redirect URI on your OAuth client.
2. Open the OAuth Playground, click the gear icon, check **Use your own OAuth credentials**, and paste your Client ID and Client Secret.
3. In the left panel, authorize `https://www.googleapis.com/auth/gmail.modify`.
4. Click **Authorize APIs**, sign in with the target Gmail account, and grant access.
5. Click **Exchange authorization code for tokens** and copy the **Refresh token** value.

To create the connector through the API instead, see [API Management](/docs/connectors#api-management); the `config` object takes the fields in the table below.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `client_id` | string | Yes | Google OAuth 2.0 client ID. Filled in for you by **Connect with Google**. |
| `client_secret` | string | Yes | Google OAuth 2.0 client secret. Filled in for you by **Connect with Google**. |
| `refresh_token` | string | Yes | OAuth 2.0 refresh token. Filled in for you by **Connect with Google**. |
| `watch_inbox` | string | No | `true` to check `labels` for new mail and fire `email_received` triggers for automations. Default `false`. Never starts chats or auto-replies. |
| `labels` | string | No | Comma-separated Gmail label IDs to watch. Defaults to `INBOX`. |
| `max_results` | string | No | Maximum number of messages to fetch per check. |
| `poll_interval_seconds` | string | No | Seconds between inbox checks. Defaults to `60`. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all. Valid values: `compose`, `read`, `organize`. |

## Capabilities

The Gmail connector reports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `read_receipts` | Yes | The connector tracks which messages it has already seen. |
| `file_upload` | Yes | File upload is advertised as a capability. |
| `rich_text` | No | Outgoing emails are sent as plain text (`text/plain`). |
| `typing_indicator` | No | Not applicable for email. |
| `reactions` | No | Not applicable for email. |
| `threads` | No | Not advertised as a connector capability (though replies preserve the Gmail thread). |
| `buttons` | No | Not supported. |

### How email triggers work

Inbox watching is off by default. With `watch_inbox` set to `true`, the connector checks Gmail every `poll_interval_seconds` (default 60) for unread messages in the configured `labels` (default `INBOX`). Each message it has not seen before fires an `email_received` trigger with the sender, subject, snippet, message ID and labels. Use it to start an [automation](/docs/features/automations). The email is never sent into a chat.

### AI Actions

See [How AI actions work](/docs/connectors#how-ai-actions-work). While a Gmail connector is active, every agent session gets its tools.

Your agent can use 15 actions: send, reply and draft email, search with Gmail query syntax, list and read messages, profile info, label management, mark read/unread, archive, and trash/untrash.

Nothing permanently deletes mail: `trash_message` moves a message to Trash (reversible with `untrash_message`; Gmail purges Trash after ~30 days), and there is no hard-delete action at all.

### What your agent can do with Gmail

- **Send, reply, and draft** -- Compose new plain-text mail, reply within threads, or leave drafts for human review.
- **Search and read** -- Full Gmail query syntax (`from:alice is:unread newer_than:7d`) plus full-body reads of individual messages.
- **Organize** -- Create and apply labels, mark read/unread, archive, and trash/untrash messages.
- **Reply in-thread** -- Reply to a message; the connector sets the proper threading headers so the reply stays in the original Gmail conversation.

## Troubleshooting

### "Access blocked: This app's request is invalid" or redirect_uri_mismatch

- **Redirect URI mismatch.** The **Authorized redirect URIs** on your Google OAuth client must include the exact URI shown under **Google app setup (administrator)**. If you open AI Shuffle at a different address, the URI changes. For the manual OAuth Playground path, add `https://developers.google.com/oauthplayground`.
- **Wrong credential type.** Use a **Web application** OAuth client.

### "Google hasn't verified this app" warning

This is expected for apps in Testing mode. Click **Advanced**, then continue to the app. For production use, you would need to submit the app for Google's verification, but this is not necessary for personal use.

### "Error 403: access_denied" during authorization

- **Test user not added.** While the app is in Testing mode, only users explicitly added as test users can authorize. Go to **Google Cloud Console > APIs & Services > OAuth consent screen > Test users** and add your Gmail address.
- **Wrong account.** Ensure you are signing in with the Gmail account that was added as a test user.

### "Google did not grant the required Gmail permission"

Google did not grant the `gmail.modify` scope. Add it on the OAuth consent screen (step 3), then click **Connect with Google** again and allow all requested access.

### "Google did not return offline access"

Remove AI Shuffle's access in your Google account (**myaccount.google.com > Security > Third-party apps with account access**), then connect again.

### Token refresh fails or "invalid_grant" error

The connector mints access tokens from the stored refresh token. If that fails:

- **Token revoked or expired.** If you revoked access in your Google account settings, or the refresh token expired (refresh tokens for apps in Testing mode expire after about 7 days), click **Reconnect with Google** on the connection. If you entered the token manually, obtain a new one and update `refresh_token`.
- **Credentials regenerated.** If you regenerated the client secret in Google Cloud Console, the existing refresh token is invalidated. Save the new secret under **Google app setup (administrator)** and reconnect.
- **Clock skew.** OAuth token validation is time-sensitive. Ensure the system clock on your AI Shuffle host is accurate (within a few seconds of actual time).

### Emails send but with wrong "From" address

- The Gmail API sends email from the connected account. The connector always sends from that account.

### Rate limiting or quota errors

The Gmail API has usage quotas:

- **250 quota units per user per second** (most operations cost 5-50 units)
- **Daily sending limit:** 500 emails/day for regular Gmail, 2,000/day for Google Workspace

If your agent makes many rapid requests, you may hit these limits. Space out operations. Check your quota usage in the [Google Cloud Console > APIs & Services > Gmail API > Quotas](https://console.cloud.google.com/apis/api/gmail.googleapis.com/quotas).
