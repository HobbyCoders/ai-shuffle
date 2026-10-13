<!--
title: Microsoft Teams
description: Register a Microsoft Teams bot with AI Shuffle (messages not supported yet)
order: 7
-->

# Microsoft Teams

**Not usable yet.** You can register a Teams bot and save its details in AI Shuffle, but it cannot send or receive messages yet. Messages sent in Teams do not reach your agent, and your agent cannot post in Teams.

If you want the bot ready for later, the steps are below.

## You need

- A [Microsoft Azure account](https://azure.microsoft.com/). The free tier is enough.
- A Microsoft 365 tenant with Teams. A free [developer tenant](https://developer.microsoft.com/en-us/microsoft-365/dev-program) works.
- Permission to register apps in Azure, or an admin who can.

## Set up

### 1. Register an app

1. In the [Azure Portal](https://portal.azure.com/), open **App registrations** and choose **New registration**.
2. Name it, for example "AI Shuffle Teams Bot". Choose **Single tenant** (only your organization) or **Multitenant**. Leave **Redirect URI** empty.
3. Choose **Register** and copy the **Application (client) ID**. For single tenant, also copy the **Directory (tenant) ID**.

### 2. Create a client secret

Open **Certificates & secrets**, choose **New client secret**, pick an expiry and choose **Add**. Copy the **value** right away: it is shown only once.

### 3. Create the Azure Bot

1. Search for **Azure Bot** and choose **Create**.
2. Enter a bot handle, your subscription and resource group, and the **F0 (Free)** pricing tier.
3. Under **Microsoft App ID**, choose **Use existing app registration** and enter the ID from step 1.
4. Choose **Review + Create**, then **Create**.
5. In the bot's **Configuration**, set **Messaging endpoint** to `https://your-domain.com/api/v1/connectors` and choose **Apply**.
6. Under **Channels**, add **Microsoft Teams** and agree to the terms.

### 4. Save it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Microsoft Teams**. Choose **Install Microsoft Teams** if asked.
2. Choose **Connect account** and enter the client ID, client secret and, for single tenant, the tenant ID.
3. Add at least one **Allowed User ID** (an Azure AD user ID).
4. Choose **Create Connector**.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `app_id` | Yes | The Application (client) ID |
| `app_password` | Yes | The client secret. Renew it before it expires |
| `tenant_id` | No | Your tenant ID, for single-tenant apps |
| `allowed_user_ids` | Yes | The Azure AD users allowed to use the bot |

## Troubleshooting

**The bot does not answer in Teams.** This is expected: Teams messages are not supported yet.

**"Unauthorized" errors.** The client secret may have expired. Create a new one under **Certificates & secrets** and update the connection. For single-tenant apps, check the tenant ID.
