"""Personal finance through Plaid. Agent actions are strictly read-only."""
from datetime import date

import httpx

from app.core.connectors.base import ActionDef, BaseConnector, ConnectorCapabilities, ConnectorStatus

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
READ_PATHS = {"accounts": "/accounts/get", "balances": "/accounts/balance/get", "transactions": "/transactions/get"}


async def plaid_request(config, path, **payload):
    host = HOSTS.get(config.get("environment"))
    if not host:
        raise ValueError("Choose a supported Plaid environment")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(host + path, json={
                **payload, "client_id": config["client_id"], "secret": config["client_secret"],
            })
        if response.is_error:
            try:
                code = response.json().get("error_code")
            except ValueError:
                code = None
            if code == "ITEM_LOGIN_REQUIRED":
                raise ValueError("Bank authorization expired. Reconnect this institution through Plaid Link.")
            if code == "PRODUCT_NOT_READY":
                raise ValueError("Transactions are still being prepared by your bank. Try again later.")
            raise ValueError("Plaid could not complete the request. Check the connection and app configuration.")
        return response.json()
    except (httpx.HTTPError, KeyError):
        # Never propagate request bodies, access tokens, or provider error text.
        raise ValueError("Plaid is unavailable or this connection is not configured") from None


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


class PlaidConnector(BaseConnector):
    CAPABILITIES = ConnectorCapabilities(is_messaging=False)
    SUPPORTS_ACTION_GATEWAY = True
    ACTIONS = [
        ActionDef(id="accounts", name="List Accounts", description="Linked bank and credit-card accounts, masked numbers and cached balances. No full account or card numbers.", input_schema=schema(), category="read", risk="safe"),
        ActionDef(id="balances", name="Read Balances", description="Fetch current available/current balances from the institution (may incur Plaid usage fees).", input_schema=schema(), category="read", risk="safe"),
        ActionDef(id="transactions", name="Read Transactions", description="A page of bank/card transactions. Positive amounts are money spent; negative amounts are credits/refunds. Pending entries may change. Follow total_transactions using offset; bank data can be delayed.", input_schema=schema({
            "start_date": {"type": "string", "description": "Inclusive ISO date YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "Inclusive ISO date YYYY-MM-DD"},
            "count": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0},
        }, ["start_date", "end_date"]), category="read", risk="safe"),
    ]

    async def _read(self, action, **payload):
        return await plaid_request(self.config, READ_PATHS[action], access_token=self.config["access_token"], **payload)

    async def _do_connect(self):
        await self._read("accounts")

    async def disconnect(self):
        self._status = ConnectorStatus.DISCONNECTED

    async def send_message(self, message):
        return False

    async def send_permission_prompt(self, platform_chat_id, prompt):
        return False

    async def send_notification(self, platform_chat_id, event_type, data):
        return False

    async def execute_action(self, action_id, params, *, context=None):
        if action_id not in READ_PATHS:
            raise ValueError("This plugin only supports reading accounts, balances and transactions")
        payload = {}
        if action_id == "transactions":
            start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
            if start > end:
                raise ValueError("start_date must not be after end_date")
            count, offset = params.get("count", 50), params.get("offset", 0)
            if type(count) is not int or not 1 <= count <= 100 or type(offset) is not int or offset < 0:
                raise ValueError("count must be 1–100 and offset must be nonnegative")
            payload = {"start_date": start.isoformat(), "end_date": end.isoformat(), "options": {"count": count, "offset": offset}}
        result = await self._read(action_id, **payload)
        # Explicit projection avoids exposing Item credentials/identifiers or
        # unexpected provider fields to the agent.
        accounts = [{k: a.get(k) for k in ("account_id", "name", "official_name", "type", "subtype", "mask", "balances")} for a in result.get("accounts", [])]
        output = {"accounts": accounts, "environment": self.config["environment"]}
        if action_id == "transactions":
            keys = ("transaction_id", "account_id", "name", "merchant_name", "amount", "iso_currency_code", "unofficial_currency_code", "date", "authorized_date", "pending", "pending_transaction_id", "personal_finance_category")
            output.update(transactions=[{k: t.get(k) for k in keys} for t in result.get("transactions", [])], total_transactions=result.get("total_transactions", 0), offset=payload["options"]["offset"])
        return output
