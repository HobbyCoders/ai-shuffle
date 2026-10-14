<!--
title: Microsoft Teams
description: Microsoft Teams is not available yet
order: 7
-->

# Microsoft Teams

**Not available yet.** Microsoft Teams is not offered in **Settings → Plugins → Apps**, and installing its runtime or connecting a new Teams account is blocked. Do not create an Azure app or client secret for AI Shuffle yet.

The connector code is retained for future development, but the app does not have an authenticated Bot Framework messaging endpoint. Messages sent in Teams do not reach your agent, and your agent cannot post in Teams.

Existing saved connections are not deleted by this change. They still cannot send or receive messages. Remove saved Teams connections before removing an already-installed Teams plugin.

Teams will be offered only after authenticated inbound messages, conversation references and outbound replies work end to end. Setup instructions will be published with that support.
