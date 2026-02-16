<!--
title: Microsoft Teams
description: Connect AI Shuffle to Microsoft Teams using Azure Bot Framework for enterprise communication
order: 7
-->

# Microsoft Teams Connector

## Overview

The Microsoft Teams connector integrates your AI Shuffle agent with Microsoft Teams, the enterprise collaboration platform used by millions of organizations worldwide. Through the Azure Bot Framework, your agent can respond to messages in Teams channels and one-on-one chats, participate in threaded conversations, share files, and display typing indicators -- all within the Teams interface your users already know.

Setting up the Teams connector involves registering an application in Azure Active Directory (Azure AD), creating a Bot Framework registration, and configuring the bot for the Teams channel. The connector communicates with Teams through the Bot Framework REST API, receiving messages via webhook and sending responses back through the same channel. This connector supports both multi-tenant deployments (accessible to any organization) and single-tenant deployments (restricted to a specific Azure AD tenant).

## Prerequisites

- A [Microsoft Azure account](https://azure.microsoft.com/) (free tier is sufficient)
- A Microsoft 365 tenant with Teams enabled (a [developer tenant](https://developer.microsoft.com/en-us/microsoft-365/dev-program) is available for free)
- Admin access to the Azure portal for app registration (or have an admin perform the registration)
- AI Shuffle backend running with a **publicly reachable HTTPS URL** (required for Bot Framework webhook delivery)
- A verified domain or tunneling service (e.g., ngrok) for development

## Setup Guide

### Step 1: Register an Azure AD Application

1. Go to the [Azure Portal](https://portal.azure.com/) and sign in.
2. Navigate to **Azure Active Directory > App registrations** (or search for "App registrations" in the top search bar).
3. Click **New registration**.
4. Fill in the registration form:
   - **Name**: Enter a name like "AI Shuffle Teams Bot"
   - **Supported account types**: Choose based on your deployment:
     - **Single tenant** -- Only users in your Azure AD directory can use the bot
     - **Multitenant** -- Users in any Azure AD directory can use the bot
   - **Redirect URI**: Leave blank (not needed for bot authentication)
5. Click **Register**.
6. On the overview page, copy the **Application (client) ID**. This is your `app_id`.
   ```
   Example: a1b2c3d4-e5f6-7890-abcd-ef1234567890
   ```
7. If using single-tenant mode, also copy the **Directory (tenant) ID**:
   ```
   Example: f1e2d3c4-b5a6-7890-abcd-1234567890ab
   ```

### Step 2: Create a Client Secret

1. In the left sidebar of your app registration, click **Certificates & secrets**.
2. Under **Client secrets**, click **New client secret**.
3. Enter a description (e.g., "AI Shuffle Bot Secret") and select an expiration period.
4. Click **Add**.
5. **Immediately copy the secret value** (it is shown only once). This is your `app_password`.
   ```
   Example: EXAMPLE-AZURE-CLIENT-SECRET
   ```

### Step 3: Create a Bot Framework Registration

1. In the Azure Portal, search for **Azure Bot** and click **Create**.
2. Fill in the bot details:
   - **Bot handle**: A unique identifier for the bot (e.g., "ai-shuffle-bot")
   - **Subscription**: Select your Azure subscription
   - **Resource group**: Create a new one or use an existing one
   - **Pricing tier**: Select **F0 (Free)** for development or **S1 (Standard)** for production
   - **Microsoft App ID**: Select **Use existing app registration** and enter the Application ID from Step 1
3. Click **Review + Create**, then **Create**.

### Step 4: Configure the Messaging Endpoint

1. After the Azure Bot resource is created, navigate to it.
2. In the left sidebar, click **Configuration**.
3. Set the **Messaging endpoint** to your AI Shuffle webhook URL:
   ```
   https://your-domain.com/api/connectors/msteams/webhook
   ```
4. Click **Apply**.

### Step 5: Enable the Teams Channel

1. In the Azure Bot resource, click **Channels** in the left sidebar.
2. Click **Microsoft Teams** in the available channels list.
3. Review the Terms of Service and click **Agree**.
4. On the configuration page, select the appropriate calling options (default settings are fine for messaging).
5. Click **Apply**.

### Step 6: Install the Bot in Teams

**For development/testing:**

1. In the Azure Bot Channels page, click **Open in Teams** (under the Microsoft Teams channel) to start a one-on-one chat with the bot.

**For organization-wide deployment:**

1. Create a Teams app manifest. Create a file named `manifest.json`:
   ```json
   {
     "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
     "manifestVersion": "1.16",
     "version": "1.0.0",
     "id": "YOUR_APP_ID",
     "developer": {
       "name": "Your Organization",
       "websiteUrl": "https://your-domain.com",
       "privacyUrl": "https://your-domain.com/privacy",
       "termsOfUseUrl": "https://your-domain.com/terms"
     },
     "name": {
       "short": "AI Shuffle",
       "full": "AI Shuffle Teams Bot"
     },
     "description": {
       "short": "AI assistant powered by AI Shuffle",
       "full": "An AI assistant that responds to messages in Teams channels and chats."
     },
     "icons": {
       "outline": "outline.png",
       "color": "color.png"
     },
     "bots": [
       {
         "botId": "YOUR_APP_ID",
         "scopes": ["personal", "team", "groupChat"],
         "commandLists": []
       }
     ],
     "permissions": ["identity", "messageTeamMembers"]
   }
   ```
2. Replace `YOUR_APP_ID` with the Application ID from Step 1.
3. Add 32x32 (`outline.png`) and 192x192 (`color.png`) icon files.
4. Zip the manifest and icons into a `.zip` file.
5. In Teams, go to **Apps > Manage your apps > Upload an app** and upload the zip file.

### Step 7: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and navigate to **Settings > Connectors**.
2. Click **Add Connector** and select **Microsoft Teams**.
3. Fill in the configuration fields:
   - **App ID**: The Application (client) ID from Step 1
   - **App Password**: The client secret from Step 2
   - **Tenant ID** (optional): The Directory (tenant) ID, required for single-tenant deployments
4. Click **Save** and then **Enable** the connector.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `app_id` | string | Yes | The Application (client) ID from the Azure AD app registration. This identifies the bot to the Bot Framework. |
| `app_password` | string | Yes | The client secret value from the Azure AD app registration. Used to authenticate the bot with the Bot Framework. Keep this value secure and rotate it before expiration. |
| `tenant_id` | string | No | The Azure AD Directory (tenant) ID. Required when the app is registered as single-tenant. For multi-tenant bots, this can be omitted or set to `common`. |

## Capabilities

The Microsoft Teams connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | Yes | Shows a typing indicator in the chat while the agent processes a response |
| Reactions | No | Teams reactions use a different mechanism than standard Bot Framework activities |
| Rich text | Yes | Supports Adaptive Cards, HTML subset, and Markdown formatting in messages |
| File upload | Yes | Send files as attachments. File size limits depend on the Teams/SharePoint configuration (default 250 MB). |
| Threads | Yes | Can reply to specific messages in channel conversations, maintaining threaded discussions |
| Buttons | No | Adaptive Cards can include buttons but require additional card schema configuration |
| Mentions | Yes | The bot can @mention users in its responses |

**Message Length Limit:** 28,000 characters per message. This is significantly higher than most other platforms, allowing for detailed responses. Messages exceeding this limit are automatically split.

## Troubleshooting

### Bot does not respond to messages in Teams

- **Messaging endpoint not configured.** Verify the messaging endpoint URL is set correctly in the Azure Bot Configuration (Step 4). The URL must be publicly reachable over HTTPS.
- **App ID or password mismatch.** Ensure the `app_id` and `app_password` in the connector configuration exactly match the Azure AD app registration values.
- **Teams channel not enabled.** Verify that the Microsoft Teams channel is enabled in the Azure Bot Channels settings (Step 5).
- **Bot not installed in Teams.** The bot must be installed in the Teams client before users can interact with it. Use the "Open in Teams" link or deploy the app manifest (Step 6).

### "Unauthorized" or 401 errors

- **Client secret expired.** Azure AD client secrets have an expiration date. Go to the app registration in Azure Portal, navigate to **Certificates & secrets**, and create a new secret. Update the `app_password` in the connector configuration.
- **Wrong tenant configuration.** If using a single-tenant app, the `tenant_id` must be set correctly. For multi-tenant apps, ensure the app registration has "Accounts in any organizational directory" selected under **Supported account types**.

### Bot works in one-on-one chat but not in channels

- **Bot not added to the team.** In Teams, go to the team where you want the bot, click **Manage team > Apps**, and add the bot.
- **Missing scopes in manifest.** Ensure the `scopes` array in the app manifest includes `"team"` in addition to `"personal"` and `"groupChat"`.
- **Permissions issue.** The Teams admin may have restricted which apps can be installed. Contact your Teams administrator.

### "BadRequest" or message format errors

- Teams has strict requirements for message formatting. If using Adaptive Cards, validate your card JSON against the [Adaptive Card Designer](https://adaptivecards.io/designer/). Ensure all required fields are present and properly formatted.
- HTML formatting in Teams messages supports only a subset of HTML tags. Unsupported tags are stripped. Stick to basic tags: `<b>`, `<i>`, `<a>`, `<pre>`, `<code>`, and `<br>`.

### Webhook validation fails during setup

- The messaging endpoint must respond to Bot Framework authentication challenges. Ensure:
  - The URL uses HTTPS with a valid (non-self-signed) SSL certificate.
  - The AI Shuffle backend is running and the Teams connector is enabled before setting the endpoint URL.
  - There are no firewall rules blocking incoming requests from Azure IP ranges.

### Bot is slow to respond

- The Bot Framework expects a response within 15 seconds. If the agent takes longer to process, the connector sends a typing indicator and holds the connection. For very long operations:
  - The connector will send an initial acknowledgment and follow up with the full response.
  - If the 15-second window is consistently exceeded, consider optimizing agent response times or implementing proactive messaging.

### Tenant restrictions prevent bot installation

- Some organizations restrict which third-party apps can be installed in Teams. The Teams admin can:
  1. Go to the **Teams admin center > Manage apps**.
  2. Search for the bot app and allow it.
  3. Alternatively, add it to an app setup policy assigned to the relevant users.
