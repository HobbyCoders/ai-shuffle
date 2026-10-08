<!--
title: Gmail
description: Give your agent Gmail tools to send, search, read and organize email
order: 13
-->

# Gmail

Let your agent send, search, read and organize your Gmail when you ask it to. Gmail is not a chat bot: connecting it never starts chats or sends automatic replies.

You can also have new mail start an [automation](/docs/features/automations).

## You need

- A Google account with Gmail.
- A free Google Cloud project, which the owner sets up once.
- AI Shuffle open in your browser over HTTPS, or on `localhost`, so Google can send you back after you sign in.

## Set up

### 1. Create a Google Cloud project

1. In the [Google Cloud Console](https://console.cloud.google.com/), open the project menu and choose **New Project**. Name it, for example `AI Shuffle Gmail`.
2. Open **APIs & Services → Library**, find **Gmail API** and choose **Enable**.

### 2. Set up the consent screen

1. Open **APIs & Services → OAuth consent screen** and choose **External**.
2. Enter the app name `AI Shuffle` and your email for support and contact.
3. Under **Scopes**, add `https://www.googleapis.com/auth/gmail.modify`. It is the only one AI Shuffle needs.
4. Under **Test users**, add your Gmail address.

Leaving the app in **Testing** is fine for personal use.

### 3. Copy the redirect address from AI Shuffle

1. Open AI Shuffle at the address you will normally use.
2. Open **Settings → Plugins**, choose **Apps** and open **Gmail**. Choose **Install Gmail** if asked.
3. Choose **Connect account**, then **Google app setup (administrator)**.
4. Copy the **Authorized redirect URI**.

The address depends on how you opened AI Shuffle. Use the same address when you set up and when you connect.

### 4. Create the sign-in client

1. In Google Cloud, open **APIs & Services → Credentials** and choose **Create Credentials → OAuth client ID**.
2. Choose **Web application** and add the redirect address from step 3 under **Authorized redirect URIs**.
3. Choose **Create** and copy the **Client ID** and **Client Secret**.
4. Back in **Google app setup (administrator)**, paste both and choose **Save Google app settings**.

You only do this once. Whoever runs the server can set these with the `SHUFFLE_GOOGLE_CLIENT_ID`, `SHUFFLE_GOOGLE_CLIENT_SECRET` and `SHUFFLE_GOOGLE_REDIRECT_URI` environment variables instead.

### 5. Connect your account

1. Choose **Connect with Google** and sign in with your Gmail account. Allow pop-ups if the window is blocked.
2. Allow access.
3. Turn on the connection's **Activate** switch.

To connect again later, for example after removing access, edit the connection and choose **Reconnect with Google** with the same account. For another account, add another connection.

Try it in a chat: "Search my inbox for unread mail from the last 7 days and summarize it."

## Settings

Set these under **Advanced settings**.

| Setting | Required | What it does |
| --- | --- | --- |
| `watch_inbox` | No | `true` checks for new mail and starts automations with the `email_received` trigger. Default `false` |
| `labels` | No | Gmail labels to watch, comma separated. Default `INBOX` |
| `max_results` | No | The most messages fetched per check |
| `poll_interval_seconds` | No | Seconds between checks. Default `60` |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `compose`, `read`, `organize`. Empty allows all |

You can also enter a client ID, client secret and refresh token yourself under **Advanced settings**, for example from the [Google OAuth Playground](https://developers.google.com/oauthplayground/).

## What your agent can do

While Gmail is on, every chat can use it. See [How AI actions work](/docs/connectors#how-ai-actions-work).

- **Write:** send, reply in the same thread and save drafts. Email is sent as plain text.
- **Read:** search with Gmail search terms such as `from:alice is:unread newer_than:7d`, and read whole messages.
- **Organize:** create and apply labels, mark read or unread, archive, and move to Trash or back.

Nothing deletes mail permanently. Trash can be undone until Gmail empties it, after about 30 days.

## Troubleshooting

**"redirect_uri_mismatch" or "This app's request is invalid".** Your Google client's redirect address must match the one in **Google app setup (administrator)** exactly. Opening AI Shuffle at another address changes it. The client type must be **Web application**.

**"Google hasn't verified this app".** Normal in Testing mode. Choose **Advanced** and continue.

**"Error 403: access_denied".** Add your Gmail address as a test user, and sign in with that account.

**"Google did not grant the required Gmail permission".** Add the `gmail.modify` scope (step 2), connect again and allow everything asked.

**"Google did not return offline access".** Remove AI Shuffle under **myaccount.google.com → Security → Third-party apps with account access**, then connect again.

**"invalid_grant" or the connection stops working.**

- In Testing mode, Google ends sign-ins after about 7 days. Choose **Reconnect with Google**.
- If you made a new client secret in Google Cloud, save it in **Google app setup (administrator)** and reconnect.
- Check that the server's clock is correct.

**Quota errors.** Gmail limits how fast you can make requests and how much you can send: 500 emails a day for Gmail, 2,000 for Google Workspace.
