<!--
title: Gmail
description: Send and receive emails through Claude via the Gmail API
order: 13
-->

# Gmail Connector

## Overview

The Gmail connector integrates AI Shuffle with your Gmail account, allowing Claude to read, compose, and send emails on your behalf. With this connector, Claude can check your inbox, summarize unread messages, draft replies, send new emails, search through your mail, and handle attachments -- all through natural language conversation.

The connector uses the official [Gmail API](https://developers.google.com/gmail/api) with OAuth 2.0 for secure authentication. Your credentials never pass through third-party servers; the OAuth flow runs directly between your browser, Google's auth servers, and your AI Shuffle instance. After the initial authorization, the connector stores a refresh token locally and automatically handles token renewal.

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

### 5. Prepare the credentials JSON

Open the downloaded JSON file. It will look something like:

```json
{
  "installed": {
    "client_id": "123456789-abcdefg.apps.googleusercontent.com",
    "project_id": "ai-shuffle-gmail",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_secret": "GOCSPX-AbCdEfGhIjKlMn",
    "redirect_uris": ["http://localhost"]
  }
}
```

You will provide this entire JSON structure (or the file path) as the `credentials_json` config value.

### 6. Create the Gmail connector in AI Shuffle

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Gmail",
    "connector_type": "gmail",
    "config": {
      "credentials_json": "{\"installed\":{\"client_id\":\"123456789-abcdefg.apps.googleusercontent.com\",\"project_id\":\"ai-shuffle-gmail\",\"auth_uri\":\"https://accounts.google.com/o/oauth2/auth\",\"token_uri\":\"https://oauth2.googleapis.com/token\",\"client_secret\":\"GOCSPX-AbCdEfGhIjKlMn\",\"redirect_uris\":[\"http://localhost\"]}}"
    }
  }'
```

Or use the AI Shuffle UI: **Settings > Connectors > Add Connector > Gmail**.

### 7. Complete the OAuth authorization flow

On first activation, the connector will initiate an OAuth flow:

1. Activate the connector (step 8 below)
2. A browser window will open (or a URL will be provided) pointing to Google's authorization page
3. Sign in with the Gmail account you want to connect
4. Review the permissions and click **Allow**
5. If you see a warning "Google hasn't verified this app", click **Advanced** then **Go to AI Shuffle (unsafe)**. This warning appears because your app is in Testing mode.
6. After authorization, a `token_json` will be auto-generated and stored in the connector config. You do not need to manage this manually.

### 8. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

### 9. Test the connector

Try these commands in an AI Shuffle chat:

- "Check my inbox for unread emails"
- "Summarize the last 5 emails"
- "Send an email to alice@example.com with the subject 'Meeting Tomorrow' and let her know the meeting is at 3 PM"
- "Search my email for messages from Bob about the project proposal"

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `credentials_json` | string | Yes | The OAuth 2.0 client credentials JSON downloaded from Google Cloud Console. Can be the raw JSON string or a file path to the downloaded JSON file. |
| `token_json` | string | No | The OAuth token JSON, auto-generated after the first successful authorization. Contains access and refresh tokens. You should not need to set this manually. |

## Capabilities

The Gmail connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `rich_text` | Yes | Email content is rendered with formatting. Responses include structured email information (subject, sender, date, body). |
| `file_upload` | Yes | Claude can send emails with file attachments |
| `typing_indicator` | No | Not applicable for email |
| `reactions` | No | Not applicable for email |
| `threads` | No | Not supported as a connector capability (though Gmail threads are handled in email display) |
| `buttons` | No | Not supported |

### What Claude can do with Gmail

- **Read emails** -- Fetch and summarize messages from your inbox or specific labels
- **Search** -- Find emails by sender, subject, date range, keywords, or Gmail search operators
- **Compose and send** -- Write new emails with subject, body, recipients (to, cc, bcc)
- **Reply and forward** -- Reply to existing email threads or forward messages
- **Attachments** -- Send files as email attachments
- **Labels** -- View, apply, and remove Gmail labels
- **Drafts** -- Create and manage email drafts
- **Mark as read/unread** -- Update the read status of messages

## Troubleshooting

### OAuth flow shows "Access blocked: This app's request is invalid"

- **Redirect URI mismatch.** Ensure the redirect URI in your Google Cloud OAuth client settings matches what the connector expects. For Desktop app credentials, the redirect URI should include `http://localhost`.
- **Wrong credential type.** Make sure you created a "Desktop app" OAuth client, not "Web application" (unless your setup specifically requires it).

### "Google hasn't verified this app" warning

This is expected for apps in Testing mode. Click **Advanced**, then **Go to AI Shuffle (unsafe)**. This warning will only appear during the initial authorization. For production use, you would need to submit the app for Google's verification, but this is not necessary for personal use.

### "Error 403: access_denied" during authorization

- **Test user not added.** While the app is in Testing mode, only users explicitly added as test users can authorize. Go to **Google Cloud Console > APIs & Services > OAuth consent screen > Test users** and add your Gmail address.
- **Wrong account.** Ensure you are signing in with the Gmail account that was added as a test user.

### Token refresh fails or "invalid_grant" error

- **Token revoked.** If you revoked access in your Google account settings (**myaccount.google.com > Security > Third-party apps with account access**), you need to re-authorize. Deactivate the connector, remove the `token_json` from the config, and reactivate to trigger a new OAuth flow.
- **Credentials regenerated.** If you regenerated the client secret in Google Cloud Console, the existing token is invalidated. Update `credentials_json` with the new credentials and re-authorize.
- **Clock skew.** OAuth token validation is time-sensitive. Ensure the system clock on your AI Shuffle host is accurate (within a few seconds of actual time).

### Emails send but with wrong "From" address

- The Gmail API sends emails from the authenticated account. You cannot spoof the sender address. If you need to send from a different address, configure a "Send as" alias in Gmail settings and specify it in the email composition.

### Rate limiting or quota errors

The Gmail API has usage quotas:

- **250 quota units per user per second** (most operations cost 5-50 units)
- **Daily sending limit:** 500 emails/day for regular Gmail, 2,000/day for Google Workspace

If Claude is making many rapid requests, you may hit these limits. Space out operations or use batch requests where possible. Check your quota usage in the [Google Cloud Console > APIs & Services > Gmail API > Quotas](https://console.cloud.google.com/apis/api/gmail.googleapis.com/quotas).
