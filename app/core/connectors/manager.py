"""
ConnectorManager — lifecycle management, event dispatch, and message routing.
"""

import asyncio
import logging
import secrets
import time
import uuid
import weakref
from typing import Any, Dict, List, Optional, Tuple

from app.core.connectors.base import (
    BaseConnector,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
)
from app.core.connectors.registry import get_connector_class
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# A connector turn runs as the connector's API user: its profile, project and
# model. Without one there is no owner-chosen agent to run, so the message is
# refused instead of falling back to whichever profile happens to be first.
# A client still CONNECTING this long after it was registered has a handshake
# that is not going to finish; a repeated start replaces it.
CONNECTING_STALE_SECONDS = 120.0

# Lifecycle lane (#1398). A wanted connect has no time limit; these only
# decide when a stuck disconnect is reported, and how long shutdown waits.
RUN, STOP = "run", "stop"
TEARDOWN_NOTICE_SECONDS = 10.0
SHUTDOWN_SETTLE_SECONDS = 5.0
PREVIOUS_CLIENT_NOT_STOPPED_MESSAGE = (
    "The previous client of this connection has not stopped yet. "
    "The connection moves on by itself once it does."
)

CONNECTOR_NEEDS_API_USER_MESSAGE = (
    "This connector has no API user bound, so it cannot run the agent. "
    "The owner needs to bind an API user in the connector settings."
)
CONNECTOR_NEEDS_PROFILE_MESSAGE = (
    "This connector's API user has no agent profile, so it cannot run the agent. "
    "The owner needs to give that API user a profile."
)


class _Lane:
    """One connector's lifecycle lane: the wanted state and the task moving toward it."""

    __slots__ = ("asked", "connect", "replace", "task", "waiters", "want")

    def __init__(self) -> None:
        self.want = STOP
        self.asked = 0  # bumped by every request; the lane exits once it has served the latest
        # A STOP was asked since the current pass began: the next RUN pass must
        # not keep the existing client (stop-then-run means a restart).
        self.replace = False
        self.waiters: List[Tuple[int, asyncio.Future]] = []
        self.task: Optional[asyncio.Task] = None
        self.connect: Optional[asyncio.Future] = None  # in-flight connect(), cancelled when STOP is wanted


class ConnectorManager:
    """Central hub for connector lifecycle, event dispatch, and message routing.

    Every start, stop and restart of a client goes through the connector's
    lane (``_request``): one task per connector that applies the latest wanted
    state, one transition at a time, so two operations on one connector never
    interleave, a stuck disconnect blocks only its own connector, and a
    manager status write always belongs to the client the lane is handling.
    """

    def __init__(self):
        self._connectors: Dict[str, BaseConnector] = {}
        self._registered_at: Dict[str, float] = {}  # connector_id -> time.monotonic()
        self._lock = asyncio.Lock()
        self._receiver_locks: dict[str, asyncio.Lock] = {}
        self._lanes: Dict[str, _Lane] = {}
        self._closing = False  # set by shutdown_all: no new starts
        self._discarding: set = set()  # disconnects of clients whose lane was cancelled mid-connect
        self._event_relay = None
        # Waiters hold a strong reference; idle chats leave no permanent lock entry.
        self._message_locks: weakref.WeakValueDictionary[
            tuple[str, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self, wait: bool = True) -> None:
        """Start the active connectors this install owns.

        With *wait* (app startup) each start is awaited in turn; without it
        (the network reconcile) each connector is only handed RUN, so a
        connect that never returns holds up nobody.

        Two installs can share one Postgres, so we must not both connect the
        same connector (that would duplicate every inbound reply). Each active
        connector row carries a ``runner_id``:

        * owned by us            → start it here.
        * unowned (runner NULL)  → attempt an atomic claim; start only if we win
                                   the race (self-heals pre-affinity NULL rows).
        * owned by another install → skip; the owner starts it on its own boot.
        """
        from app.core.config import settings

        me = settings.effective_instance_id
        # One isolated read; each connector is then started with an await, so
        # nothing after this can be folded into the same hop.
        connectors = await run_sync(database.get_active_connectors)
        logger.info(
            f"Evaluating {len(connectors)} active connector(s) for runner {me}"
        )

        from app.network import ownership

        for conn_row in connectors:
            cid = conn_row["id"]
            runner_id = conn_row.get("runner_id")
            try:
                # Worker network: the owner device runs the connector; an
                # unowned definition is claimed by the first device to see it,
                # and a definition owned elsewhere stays inactive here.
                network_rule = ownership.decide_connector(conn_row)
                if network_rule == "start":
                    await self._start_owned(cid, wait)
                    continue
                if network_rule == "claim":
                    if await run_sync(ownership.claim, "connectors", cid):
                        logger.info(f"Claimed unowned connector {cid} for device {ownership.my_device_id()}")
                        await self._start_owned(cid, wait)
                    else:
                        logger.info(f"Connector {cid} claimed by another device — skipping start")
                    continue
                if network_rule == "skip":
                    logger.info(f"Connector {cid} is owned by device {conn_row.get('owner_device_id')} — skipping start")
                    continue
                if runner_id == me:
                    await self._start_owned(cid, wait)
                elif runner_id is None:
                    # Race-safe claim: only the install whose UPDATE flips the
                    # NULL owner starts it; losers skip.
                    # Its own hop: the claim decides whether the ``await``
                    # below runs, so it cannot be batched with anything.
                    if await run_sync(database.claim_connector_runner, cid, me):
                        logger.info(f"Claimed unowned connector {cid} for runner {me}")
                        await self._start_owned(cid, wait)
                    else:
                        logger.info(
                            f"Connector {cid} claimed by another runner — skipping start"
                        )
                else:
                    logger.info(
                        f"Connector {cid} is pinned to runner {runner_id} "
                        f"(this install is {me}) — skipping start"
                    )
            except Exception as e:
                logger.error(f"Failed to start connector {cid}: {e}")

    async def _start_owned(self, connector_id: str, wait: bool) -> None:
        if wait:
            await self.start_connector(connector_id)
        else:
            self.request_run(connector_id)

    def start_event_relay(self) -> None:
        """Listen for lifecycle notifications addressed to this connector owner."""
        from app.core.config import settings
        from app.core.connectors.lifecycle_relay import LifecycleRelay

        if self._event_relay is None:
            self._event_relay = LifecycleRelay(
                settings.effective_instance_id, self._deliver_relayed_event
            )
        self._event_relay.start()

    async def shutdown_all(self) -> None:
        """Stop every connector and the lifecycle relay; refuse starts from now on.

        Every lane is asked to stop and given ``SHUTDOWN_SETTLE_SECONDS``;
        lanes still running then (a disconnect that does not return) are
        cancelled. Nothing is registered when this returns.
        """
        self._closing = True
        if self._event_relay is not None:
            await self._event_relay.stop()
            self._event_relay = None
        async with self._lock:
            connector_ids = set(self._connectors) | set(self._lanes)
        for cid in connector_ids:
            self._request(cid, STOP)
        lanes = [lane.task for lane in self._lanes.values() if lane.task is not None]
        if lanes:
            _done, pending = await asyncio.wait(lanes, timeout=SHUTDOWN_SETTLE_SECONDS)
            for task in pending:
                logger.warning(f"Connector lane {task.get_name()} did not stop in time; cancelling it")
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)
        async with self._lock:
            leftovers = list(self._connectors.items())
            self._connectors.clear()
            self._registered_at.clear()
        if leftovers:
            await asyncio.wait([asyncio.ensure_future(self._disconnect_quietly(cid, inst))
                                for cid, inst in leftovers], timeout=1.0)

        logger.info("All connectors shut down")

    def reopen(self) -> None:
        """Accept starts again after ``shutdown_all`` (app startup only; the
        process keeps this manager across app lifespans, as the tests do)."""
        self._closing = False

    def receiver_lock(self, connector_id: str) -> asyncio.Lock:
        """Serialize receiver startup with a Telegram journal transfer fence."""
        return self._receiver_locks.setdefault(connector_id, asyncio.Lock())

    # -- lifecycle lane ---------------------------------------------------

    def _request(self, connector_id: str, want: str, wait: bool = False) -> Optional[asyncio.Future]:
        """Set what *connector_id* should be and make sure its lane runs.

        Returns at once. With *wait*, returns a future the lane resolves with
        "is it running now" after the first pass that saw this request (or
        ``None`` when a start is refused because the manager is closing).
        Asking for STOP cancels a connect still in flight: an unwanted start
        may be cancelled, a wanted one never is.
        """
        if want == RUN and self._closing:
            return None
        loop = asyncio.get_running_loop()
        lane = self._lanes.get(connector_id)
        if lane is None or lane.task is None or lane.task.done() or lane.task.get_loop() is not loop:
            lane = self._lanes[connector_id] = _Lane()
            lane.task = loop.create_task(self._run_lane(connector_id, lane),
                                         name=f"connector-lane:{connector_id}")
            # Also settles a lane cancelled before its first step, whose
            # ``finally`` never runs.
            lane.task.add_done_callback(lambda _task, cid=connector_id, ln=lane: self._settle_lane(cid, ln))
        lane.want = want
        lane.asked += 1
        if want == STOP:
            lane.replace = True
            if lane.connect is not None:
                lane.connect.cancel()
        if not wait:
            return None
        waiter = loop.create_future()
        lane.waiters.append((lane.asked, waiter))
        return waiter

    def request_run(self, connector_id: str) -> bool:
        """Want *connector_id* running, without waiting. False once shutting down."""
        if self._closing:
            return False
        self._request(connector_id, RUN)
        return True

    def request_stop(self, connector_id: str) -> None:
        """Want *connector_id* stopped, without waiting."""
        self._request(connector_id, STOP)

    def held_connector_ids(self) -> set:
        """Connectors with a client here: registered, connecting or still tearing down.

        A connect in flight or a pending teardown is not registered, so a
        caller stopping "everything this worker runs" asks these, not
        ``get_running_connectors``. Ids with neither stay untouched, so a
        stop never writes "disconnected" over the status another worker's
        client reports.
        """
        return set(self._connectors) | set(self._lanes)

    async def _run_lane(self, connector_id: str, lane: _Lane) -> None:
        """Move the connector toward ``lane.want`` until a pass serves the latest request."""
        outcome: Any = False
        try:
            while True:
                asked, want, replace = lane.asked, lane.want, lane.replace
                lane.replace = False
                error: Optional[BaseException] = None
                try:
                    if want == RUN and not self._closing:
                        outcome = await self._start_pass(connector_id, lane, replace)
                    else:
                        await self._stop_pass(connector_id)
                        outcome = False
                except Exception as e:
                    logger.error(f"Connector {connector_id} lifecycle step failed: {e}")
                    outcome, error = False, e
                pending = []
                for seq, waiter in lane.waiters:
                    if seq > asked:
                        pending.append((seq, waiter))
                    elif not waiter.done():
                        if error is not None:
                            waiter.set_exception(error)
                        else:
                            waiter.set_result(outcome)
                lane.waiters = pending
                if lane.asked == asked:
                    return
        finally:
            self._settle_lane(connector_id, lane)

    def _settle_lane(self, connector_id: str, lane: _Lane) -> None:
        """Forget an ended lane and answer its waiters. Idempotent."""
        if self._lanes.get(connector_id) is lane:
            del self._lanes[connector_id]
        waiters, lane.waiters = lane.waiters, []
        for _seq, waiter in waiters:
            if not waiter.done():
                waiter.set_result(False)  # cancelled at shutdown: it is not running

    async def _teardown(self, connector_id: str, instance: BaseConnector) -> None:
        """Disconnect one unregistered client, in the lane, for as long as it takes.

        A disconnect that does not return is this connector's single pending
        teardown: the lane stays on it (so no second client starts) and the
        row says so; the lane carries on by itself when it returns.
        """
        task = asyncio.ensure_future(self._disconnect_quietly(connector_id, instance))
        try:
            done, _ = await asyncio.wait({task}, timeout=TEARDOWN_NOTICE_SECONDS)
            if not done:
                logger.warning(f"Connector {connector_id}: its previous client has not stopped yet")
                # A failed notice must not end the wait: only the lane's own
                # cancellation may give up on this disconnect.
                await self._note_status(
                    connector_id, "error",
                    status_message=PREVIOUS_CLIENT_NOT_STOPPED_MESSAGE,
                    error=PREVIOUS_CLIENT_NOT_STOPPED_MESSAGE,
                )
                await asyncio.wait({task})
        except BaseException:
            task.cancel()  # only the lane's own cancellation (shutdown) gets here
            raise

    @staticmethod
    async def _note_status(connector_id: str, status: str, **fields: Any) -> None:
        """A status write the lane must not stop for: a failure is logged, not raised."""
        try:
            await run_sync(database.update_connector_status, connector_id, status, **fields)
        except Exception as e:
            logger.warning(f"Connector {connector_id}: could not record status {status!r}: {e}")

    async def _stop_pass(self, connector_id: str) -> None:
        async with self._lock:
            instance = self._connectors.pop(connector_id, None)
            self._registered_at.pop(connector_id, None)
        if instance:
            await self._teardown(connector_id, instance)
        # Only the lane registers clients, so none is newer than this stop;
        # checked anyway so the write can never cover a live client.
        if connector_id not in self._connectors:
            await run_sync(database.update_connector_status, connector_id, "disconnected")
        logger.info(f"Connector stopped: {connector_id}")

    # -- public lifecycle calls (each waits for its lane) -------------------

    async def start_connector(self, connector_id: str) -> bool:
        """Want it running; return whether it is running once the lane has acted.

        Cancelling the caller does not cancel the lane: the connector still
        connects, because RUN is still wanted.
        """
        waiter = self._request(connector_id, RUN, wait=True)
        if waiter is None:
            logger.info(f"Connector {connector_id} not started: shutting down")
            return False
        return await waiter

    async def stop_connector(self, connector_id: str) -> None:
        """Disconnect and unregister a running connector; returns when that stop returns."""
        waiter = self._request(connector_id, STOP, wait=True)
        await waiter

    async def restart_connector(self, connector_id: str) -> bool:
        """Stop then start a connector."""
        await self.stop_connector(connector_id)
        return await self.start_connector(connector_id)

    async def _start_pass(self, connector_id: str, lane: _Lane, replace: bool = False) -> bool:
        """One RUN step of the lane: keep, or tear down and replace, the client.

        Idempotent for a live client: starting a connector that is already
        connected (or still connecting) returns ``True`` without building a
        second client, so a repeated POST /activate or MCP ``toggleConnector``
        cannot leave two clients each handling every inbound message. A
        registered client that has errored or disconnected, or that is stuck
        connecting (its client task ended, or its handshake has run past
        ``CONNECTING_STALE_SECONDS``), is torn down and replaced.
        A live client whose row now holds a different config (an edit made
        on another worker, synced here) is replaced as well (#1382).
        Config changes made here go through ``restart_connector``, which stops first.
        With *replace* (a STOP was asked since the last pass) the existing
        client is never kept, so a stop followed by a start always restarts.

        Teardowns run outside ``receiver_lock``; the lock covers the start
        rule, connect() and registration, as the journal transfer fence needs.
        """
        current_config = None
        if isinstance(getattr(self._connectors.get(connector_id), "_started_config_fingerprint", None), str):
            current_config = await run_sync(self._row_config_fingerprint, connector_id)
        async with self._lock:
            existing = self._connectors.get(connector_id)
            if (existing is not None and not replace and self._is_live(connector_id, existing)
                    and self._same_config(connector_id, existing, current_config)):
                logger.info(f"Connector already running: {connector_id}")
                return True
            if existing is not None:
                del self._connectors[connector_id]
                self._registered_at.pop(connector_id, None)
        if existing is not None:
            await self._teardown(connector_id, existing)
            if lane.want != RUN or self._closing:
                return False  # a newer decision came in; the lane applies it next
        async with self.receiver_lock(connector_id):
            started, discard = await self._connect_new(connector_id, lane)
        if discard is not None:
            await self._teardown(connector_id, discard)
        if started is None and connector_id not in self._connectors:
            # It lost its start rule while connecting, or a stop came before
            # connect() began; the row says "connecting" until this write.
            await run_sync(database.update_connector_status, connector_id, "disconnected")
        return bool(started)

    async def _connect_new(
        self, connector_id: str, lane: _Lane,
    ) -> Tuple[Optional[bool], Optional[BaseConnector]]:
        """Build, connect and register a client under ``receiver_lock``.

        Returns ``(started, discard)``: *discard* is a client the caller tears
        down after releasing the lock; ``started`` is None when it lost the
        start rule while connecting, or was abandoned before connect() began
        (the row is then stamped disconnected).
        """

        def _prepare_blocking() -> Optional[Dict[str, Any]]:
            """Row read, config decrypt and impl-class lookup in ONE hop.

            Contiguous — no awaits between them — and the unknown-type branch
            STAMPS the error status, so the decide-and-write stays inside the
            same block rather than becoming a second hop. ``None`` means the
            caller returns ``False``; the logging is unchanged.
            """
            connector_row = database.get_connector(connector_id)
            if not connector_row:
                logger.error(f"Connector not found in DB: {connector_id}")
                return None

            from app.network import ownership
            try:
                decision = ownership.decide_connector(connector_row)
                if decision == "skip":
                    return None
                if decision == "claim" and not ownership.claim("connectors", connector_id):
                    return None
            except Exception:
                logger.exception("Network ownership unavailable; refusing connector start")
                return None

            connector_type = connector_row["type"]
            if connector_type == "telegram":
                from app.db.watch_journal import receiver_allowed

                if not receiver_allowed(connector_id):
                    return None
            config = connector_row.get("config", {}) or {}

            # Decrypt sensitive configuration fields
            config = self._decrypt_config(config, connector_type)
            fingerprint = self._config_fingerprint(config)
            if connector_type == "msteams":
                from app.core.connector_service import teams_runtime_config
                config = teams_runtime_config(connector_id, config)

            # Look up the implementation class from the registry
            try:
                if config.get("runtime_plugin_id"):
                    from app.core.connector_packages import require_installed
                    require_installed(connector_type)
                cls = get_connector_class(connector_type)
            except ValueError as e:
                logger.error(f"Cannot start connector {connector_id}: {e}")
                database.update_connector_status(connector_id, "error", error=str(e))
                return None
            return {"row": connector_row, "type": connector_type,
                    "config": config, "cls": cls, "fingerprint": fingerprint}

        prepared = await run_sync(_prepare_blocking)
        if prepared is None:
            return False, None
        connector_type = prepared["type"]
        config = prepared["config"]
        cls = prepared["cls"]

        # A chat connector with no sender allowlist hands the agent to anyone
        # who finds the bot, so it does not start.
        from app.core.connector_service import (
            ALLOWLIST_REQUIRED_MESSAGE,
            missing_sender_allowlist,
        )

        if missing_sender_allowlist(connector_type, config):
            logger.error(
                "Connector %s (%s) not started: %s",
                connector_id, connector_type, ALLOWLIST_REQUIRED_MESSAGE,
            )
            await run_sync(
                database.update_connector_status,
                connector_id, "error",
                status_message=ALLOWLIST_REQUIRED_MESSAGE,
                error=ALLOWLIST_REQUIRED_MESSAGE,
            )
            return False, None
        if lane.want != RUN or self._closing:
            return False, None

        # Create the connector instance
        instance = cls(
            connector_id=connector_id,
            connector_type=connector_type,
            config=config,
            on_incoming_message=self.handle_incoming_message,
            on_permission_response=self.handle_permission_response,
            on_trigger=self._handle_trigger,
        )
        instance._started_config_fingerprint = prepared["fingerprint"]

        # Attempt to connect
        asked = lane.asked
        try:
            # The status flip has to land BEFORE connect() is awaited, so it is
            # its own hop; connect() is network work that stays on the loop.
            await run_sync(database.update_connector_status, connector_id, "connecting")
            success = await self._await_connect(connector_id, lane, instance)
            if success is None:
                # A stop (or shutdown) came during the status write. connect()
                # never ran, so the instance is dropped without a disconnect.
                logger.info(f"Connector {connector_id}: start abandoned, a stop was asked")
                return None, None
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the lane itself is cancelled (shutdown)
            if lane.asked == asked:
                # Nobody asked for anything since: connect() cancelled itself.
                logger.error(f"Connector {connector_id} connect() was cancelled")
                # Quiet: the instance still has to reach the teardown below.
                await self._note_status(connector_id, "error", error="connect() was cancelled")
            else:
                # STOP was asked while it connected: tear it down, the lane stops it.
                logger.info(f"Connector {connector_id}: start cancelled, a stop was asked")
            return False, instance
        except Exception as e:
            logger.error(f"Connector {connector_id} connect() raised: {e}")
            await run_sync(
                database.update_connector_status, connector_id, "error", error=str(e)
            )
            return False, None

        if success and (lane.want != RUN or self._closing):
            # It connected after a stop was asked, or during shutdown.
            return False, instance
        if success:
            # Only the lane registers, and it unregistered any earlier client.
            async with self._lock:
                self._connectors[connector_id] = instance
                self._registered_at[connector_id] = time.monotonic()
            # Re-apply the start rule now that the client is registered, so a
            # fence (or an ownership move, or a delete) whose reconcile ran
            # before this lane existed stops it here; network startup keeps
            # no sweep for late registrations. Checked after registering, a
            # fence landing later finds it in ``held_connector_ids``.
            if not await run_sync(self._still_eligible, connector_id):
                logger.info(f"Connector {connector_id} lost eligibility while connecting; stopping it")
                async with self._lock:
                    if self._connectors.get(connector_id) is instance:
                        del self._connectors[connector_id]
                        self._registered_at.pop(connector_id, None)
                return None, instance
            # Keep status as "connecting" — the connector's on_ready callback
            # will update to "connected" once the gateway handshake completes.
            # If login fails, _run_client will update to "error".
            logger.info(f"Connector started: {connector_id} ({connector_type})")
            return True, None
        else:
            await run_sync(
                database.update_connector_status,
                connector_id, "error", error="connect() returned False",
            )
            return False, None

    async def _await_connect(self, connector_id: str, lane: _Lane, instance: BaseConnector) -> Optional[bool]:
        """Await connect() as its own future, so a STOP request can cancel it.

        No time limit: only ``_request(STOP)`` (``lane.connect.cancel()``) or
        the lane's own cancellation at shutdown cancels it. If the lane is
        cancelled and connect() still returns True later, that client is
        disconnected rather than left running unregistered.

        Returns None, without calling connect(), when RUN is no longer wanted.
        That check and installing ``lane.connect`` have no await between them,
        so a STOP either sees the connect to cancel or is seen here.
        """
        if lane.want != RUN or self._closing:
            return None
        connect = lane.connect = asyncio.ensure_future(instance.connect())
        try:
            await asyncio.wait({connect})
        except BaseException:
            connect.cancel()
            connect.add_done_callback(lambda task: self._discard_late(connector_id, instance, task))
            raise
        finally:
            lane.connect = None
        return connect.result()  # raises CancelledError when a STOP cancelled it

    def _discard_late(self, connector_id: str, instance: BaseConnector, connect: asyncio.Future) -> None:
        if connect.cancelled() or connect.exception() is not None or not connect.result():
            return
        task = asyncio.ensure_future(self._disconnect_quietly(connector_id, instance))
        self._discarding.add(task)
        task.add_done_callback(self._discarding.discard)

    @staticmethod
    def _config_fingerprint(decrypted_config: Dict[str, Any]) -> str:
        """What a client depends on: the decrypted config, minus the owner's
        sender ids (read per message, so changing them needs no reconnect)."""
        import hashlib
        import json

        from app.core.connector_service import _without_owner_ids
        body = json.dumps(_without_owner_ids(dict(decrypted_config or {})), sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    def _row_config_fingerprint(self, connector_id: str) -> Optional[str]:
        """The fingerprint of the config the row holds now; None when it cannot be read.

        No connector writes its own ``config`` while it runs (only the
        connector routes, OAuth and the MCP tools do, and the key rotation
        rewrites the same plaintext), so a difference always means an edit.
        """
        from app.core import encryption
        row = database.get_connector(connector_id)
        if not row:
            return None  # deleted: the reconcile stops it
        config = row.get("config") or {}
        if not encryption.is_encryption_ready() and any(
                isinstance(v, str) and encryption.is_encrypted(v) for v in config.values()):
            return None  # the key is not loaded: keep the client rather than restart it without its secrets
        return self._config_fingerprint(self._decrypt_config(config, row["type"]))

    @staticmethod
    def _same_config(connector_id: str, instance: BaseConnector, current: Optional[str]) -> bool:
        started = getattr(instance, "_started_config_fingerprint", None)
        if not isinstance(started, str) or current is None or current == started:
            return True
        logger.info(f"Connector {connector_id}: its config changed; restarting its client")
        return False

    @staticmethod
    def _still_eligible(connector_id: str) -> bool:
        """The pre-connect start rule, re-read: row present and not fenced/owned elsewhere.

        ``decide_connector`` is the rule network startup and ``start_all`` use
        (None off-network, ``start`` for this device's own connector). Fails
        closed, like the check before connect().
        """
        from app.network import ownership

        try:
            row = database.get_connector(connector_id)
            if not row:
                return False
            if row["type"] == "telegram":
                from app.db.watch_journal import receiver_allowed

                if not receiver_allowed(connector_id):
                    return False
            return ownership.decide_connector(row) in (None, "start")
        except Exception:
            logger.exception("Network ownership unavailable; stopping connector %s", connector_id)
            return False

    def _is_live(self, connector_id: str, instance: BaseConnector) -> bool:
        """A registered client worth keeping: connected, or connecting in time."""
        if instance.status == ConnectorStatus.CONNECTED:
            return True
        if instance.status != ConnectorStatus.CONNECTING:
            return False
        task = getattr(instance, "_task", None)
        if task is not None and task.done():
            return False
        since = self._registered_at.get(connector_id)
        return since is None or time.monotonic() - since < CONNECTING_STALE_SECONDS

    @staticmethod
    async def _disconnect_quietly(connector_id: str, instance: BaseConnector) -> None:
        try:
            await instance.disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting connector {connector_id}: {e}")

    # ------------------------------------------------------------------
    # Trigger dispatch (connector variant triggers → automations)
    # ------------------------------------------------------------------

    async def _handle_trigger(
        self, connector_id: str, trigger_id: str, payload: Dict[str, Any]
    ) -> None:
        """Callback invoked by connectors when they emit a trigger event.

        Routes the trigger to the automation manager which will fire any
        automations configured with ``trigger_type="connector_trigger"``
        matching this connector and trigger ID.
        """
        logger.debug(
            "Connector %s emitted trigger '%s'", connector_id, trigger_id
        )
        from app.core.watch_sources import record_connector
        from app.core.watch_manager import watch_manager

        if trigger_id == "watch_message_received":
            # A non-replayable ingress acknowledgement is a commit receipt,
            # unlike the ordinary best-effort automation trigger callback.
            from app.db import watches as watch_store

            trigger_id = "message_received"
            await run_sync(
                watch_store.record_connector_event, connector_id,
                "message_received:" + str(payload["message_id"]),
                dict(payload, event_type=trigger_id),
            )
            watch_manager.notify()
        else:
            try:
                await run_sync(record_connector, connector_id, trigger_id, payload)
                watch_manager.notify()
            except Exception:
                logger.exception(
                    "Failed to record watch observation for %s/%s", connector_id, trigger_id
                )
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.handle_connector_trigger(
                connector_id, trigger_id, payload
            )
        except Exception as e:
            logger.error(
                "Failed to dispatch connector trigger %s/%s to automations: %s",
                connector_id, trigger_id, e,
            )

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def dispatch_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """
        Fan-out an event to every connector subscribed to *event_type*.

        If *data* contains a ``session_id``, only connector-sessions linked to
        that AI session are notified.  Otherwise every active session for each
        connector is sent the notification.
        """
        from app.core.config import settings
        from app.core.connectors.lifecycle_relay import publish

        subscribed = await run_sync(database.get_connectors_for_event, event_type)
        from app.network import ownership
        for conn_row in subscribed:
            if ownership.enrolled() and not ownership.owns(conn_row):
                # Another device holds this connector's session; it sees its
                # own sessions' events, so there is nothing to relay.
                continue
            owner = conn_row.get("runner_id")
            if owner and owner != settings.effective_instance_id:
                try:
                    await run_sync(publish, conn_row["id"], owner, event_type, data)
                except Exception:
                    logger.warning("Connector owner relay failed for %s", conn_row["id"], exc_info=True)
            else:
                await self._deliver_connector_event(conn_row, event_type, data)

        # Dispatch to automations triggered by connector events
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.handle_connector_event(event_type, data)
        except Exception as e:
            logger.error(f"Automation event dispatch failed: {e}")

    async def _deliver_relayed_event(self, connector_id: str, event_type: str, data: dict) -> None:
        """Recheck ownership/subscription; never re-dispatch webhooks or automations."""
        from app.core.config import settings

        row = await run_sync(database.get_connector, connector_id)
        from app.network import ownership
        if ownership.enrolled() and row and not ownership.owns(row):
            return
        if (not row or not row.get("is_active")
                or (not ownership.enrolled() and row.get("runner_id") != settings.effective_instance_id)
                or event_type not in (row.get("events") or [])):
            return
        await self._deliver_connector_event(row, event_type, data)

    async def _deliver_connector_event(self, conn_row: dict, event_type: str, data: dict) -> None:
        """Deliver to local platform chats linked to the event's session."""
        session_id = data.get("session_id")
        cid = conn_row["id"]
        log_id = secrets.token_hex(8)

        async with self._lock:
            instance = self._connectors.get(cid)

        if not instance:
            logger.debug(f"Connector {cid} not running — skipping event {event_type}")
            return

        try:
            # Determine which platform chats to notify. Either branch is a
            # blocking query (the broadcast branch opens its own cursor), so
            # the whole decision runs off-loop as one hop; exceptions still
            # land in this ``try``'s handler below.
            def _load_sessions() -> List[Dict[str, Any]]:
                if session_id:
                    rows = database.get_connector_sessions_for_ai_session(session_id)
                    # Filter to only sessions belonging to this connector
                    return [s for s in rows if s.get("connector_id") == cid]
                # Broadcast to all active sessions for this connector
                # (no dedicated DB helper — query directly)
                from app.db.base import get_db, rows_to_list
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT * FROM connector_sessions WHERE connector_id = %s AND is_active = TRUE",
                        (cid,),
                    )
                    return rows_to_list(cursor.fetchall())

            sessions = await run_sync(_load_sessions)

            if not sessions:
                logger.debug(
                    f"No connector sessions linked for connector {cid} "
                    f"event={event_type} session_id={session_id} — skipping"
                )
                return

            any_sent = False
            for sess in sessions:
                chat_id = sess.get("platform_chat_id")
                if chat_id:
                    ok = await instance.send_notification(chat_id, event_type, data)
                    if ok:
                        any_sent = True
                    else:
                        logger.warning(
                            f"send_notification failed for connector {cid} "
                            f"chat_id={chat_id} event={event_type}"
                        )

            status = "delivered" if any_sent else "failed"

            # Both writes are contiguous — one hop for the pair.
            def _log_delivery() -> None:
                database.create_connector_event_log(
                    log_id=log_id,
                    connector_id=cid,
                    event_type=event_type,
                    direction="outbound",
                    payload=data,
                    status=status,
                    error=None if any_sent else "No messages were delivered to platform",
                )
                database.update_connector_event_dispatched(cid, success=any_sent)

            await run_sync(_log_delivery)

        except Exception as e:
            logger.error(f"Event dispatch failed for connector {cid}: {e}")

            def _log_failure(err: str = str(e)) -> None:
                database.create_connector_event_log(
                    log_id=log_id,
                    connector_id=cid,
                    event_type=event_type,
                    direction="outbound",
                    payload=data,
                    status="failed",
                    error=err,
                )
                database.update_connector_event_dispatched(cid, success=False)

            await run_sync(_log_failure)

    # ------------------------------------------------------------------
    # Connector context for system prompt
    # ------------------------------------------------------------------

    @staticmethod
    def _build_connector_context(
        connector_row: Dict[str, Any],
        instance: BaseConnector,
        platform_chat_id: str,
    ) -> str:
        """Build system prompt context describing the platform and memory instructions."""
        from dataclasses import fields as dc_fields

        parts: List[str] = []

        connector_type = connector_row.get("type", "unknown")
        connector_name = connector_row.get("name", connector_type)

        # Platform identity
        parts.append(
            f"You are communicating via {connector_name} ({connector_type}). "
            f"Messages are from platform chat {platform_chat_id}."
        )

        # Memory instructions
        parts.append(
            "MEMORY: Important context about this user, ongoing projects, preferences, "
            "and key decisions should be persisted to AGENTS.md in the working directory. "
            "Read AGENTS.md at the start of each conversation to recall prior context. "
            "Update it when you learn important new information worth remembering."
        )

        # Platform capabilities
        caps = instance.CAPABILITIES
        cap_names = [
            f.name.replace("_", " ")
            for f in dc_fields(caps)
            if f.name != "is_messaging" and getattr(caps, f.name) is True
        ]
        if cap_names:
            parts.append(f"Platform capabilities: {', '.join(cap_names)}.")
        if instance.MAX_MESSAGE_LENGTH:
            parts.append(
                f"Max message length: {instance.MAX_MESSAGE_LENGTH} characters. "
                "Split long responses if needed."
            )

        # Connector-provided usage instructions (e.g. the Discord action
        # gateway blurb). Each connector owns its own guidance.
        try:
            extra = instance.get_context_instructions(platform_chat_id)
            if extra:
                parts.append(extra)
        except Exception:
            logger.debug("get_context_instructions failed", exc_info=True)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Incoming message handling
    # ------------------------------------------------------------------

    async def handle_incoming_message(self, message: IncomingMessage) -> None:
        """Serialize routing, session minting and reply delivery for one chat.

        Polling and permission callbacks do not acquire this lock. Different
        chats also remain concurrent. asyncio locks admit waiting messages FIFO.
        The owner's plain-text answer to a permission request does not wait for
        the lock, which is held by the very turn waiting for that answer. The
        message's place in the queue is still taken before anything is awaited,
        so a slow check of one message cannot let a later one overtake it.
        """
        from app.core.connector_permissions import answer_from_chat

        key = (message.connector_id, message.platform_chat_id)
        lock = self._message_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._message_locks[key] = lock
        place = asyncio.ensure_future(lock.acquire())
        try:
            if await answer_from_chat(message):
                return
            await place
            await self._handle_incoming_message(message)
        finally:
            if not place.done():
                place.cancel()
            elif not place.cancelled() and place.exception() is None:
                lock.release()

    async def _handle_incoming_message(self, message: IncomingMessage) -> None:
        """
        Process a message arriving from a platform connector.

        Resolves (or creates) a connector session and its linked AI session,
        enrols the message as a durable ``source=connector`` turn on the v2 turn
        queue, waits for that turn to settle, and sends the assistant's reply
        back through the connector.

        Going through the queue is what keeps a chatty channel safe: two
        messages seconds apart used to start two concurrent agent loops against
        the same session (a forked conversation and spend nobody accounted for).
        Now the second one queues behind the live turn and runs when it settles.
        """
        try:
            from app.network import ownership

            # Get the connector instance early for ack/typing/read
            async with self._lock:
                instance = self._connectors.get(message.connector_id)

            def _resolve_routing_blocking() -> Dict[str, Any]:
                """Connector row + API user + session lookup/creation: contiguous
                round trips, ONE thread hop.

                The chat FIFO covers this read-decide-write and the later AI
                session mint/link, including intervening awaits. Batching these
                reads saves hops; it is not itself a concurrency fence. A result with
                ``refused`` means the connector has no API user (or that user
                has no agent profile): the turn is refused rather than run on
                some other profile.
                """
                # Determine the API user for this connector
                connector_row = database.get_connector(message.connector_id)
                # A revoked connection can still deliver callbacks. Check the
                # same routing row in this hop, before any external acknowledgment.
                if ownership.enrolled() and not (connector_row and ownership.owns(connector_row)):
                    return {"not_owned": True}
                api_user_id = connector_row.get("api_user_id") if connector_row else None
                api_user = database.get_api_user(api_user_id) if api_user_id else None
                if not api_user:
                    return {"refused": CONNECTOR_NEEDS_API_USER_MESSAGE}
                profile_ids = api_user.get("profile_ids", []) or []
                # Assignments outlive deleted/retired agents (the allowlist
                # stays restricted), so run the first one that still exists.
                profile_id = next((pid for pid in profile_ids if database.get_profile(pid)), None)
                if not profile_id:
                    return {"refused": CONNECTOR_NEEDS_PROFILE_MESSAGE}

                # Look up or create connector session
                session = database.get_connector_session(
                    message.connector_id, message.platform_chat_id
                )

                if not session:
                    session_id = secrets.token_hex(8)
                    session = database.create_connector_session(
                        session_id=session_id,
                        connector_id=message.connector_id,
                        platform_chat_id=message.platform_chat_id,
                        platform_user_id=message.platform_user_id,
                        platform_user_name=message.platform_user_name,
                    )

                # Project and run config come from the same API user.
                project_id = api_user.get("project_id")
                api_user_model = api_user.get("model")
                api_user_effort = api_user.get("effort")
                return {
                    "session": session,
                    "connector_row": connector_row,
                    "api_user_id": api_user_id,
                    "profile_id": profile_id,
                    "project_id": project_id,
                    "api_user_model": api_user_model,
                    "api_user_effort": api_user_effort,
                }

            routing = await run_sync(_resolve_routing_blocking)
            if routing.get("not_owned"):
                return

            # Acknowledge receipt immediately (emoji reaction + read receipt)
            if instance and message.platform_message_id:
                try:
                    await instance.send_ack_reaction(
                        message.platform_chat_id, message.platform_message_id
                    )
                except Exception:
                    logger.debug("Ack reaction failed", exc_info=True)
                try:
                    await instance.mark_as_read(
                        message.platform_chat_id, message.platform_message_id
                    )
                except Exception:
                    logger.debug("Mark-as-read failed", exc_info=True)

            if routing.get("refused"):
                logger.error(
                    "Connector %s refused a message from chat %s: %s",
                    message.connector_id, message.platform_chat_id, routing["refused"],
                )
                if instance:
                    try:
                        await instance.send_message(OutgoingMessage(
                            platform_chat_id=message.platform_chat_id,
                            text=routing["refused"],
                        ))
                    except Exception:
                        logger.debug("Refusal reply failed", exc_info=True)
                return
            session = routing["session"]
            connector_row = routing["connector_row"]
            api_user_id = routing["api_user_id"]
            profile_id = routing["profile_id"]
            project_id = routing["project_id"]
            api_user_model = routing["api_user_model"]
            api_user_effort = routing["api_user_effort"]

            # Pre-mint + link the AI session BEFORE the turn is submitted.
            #
            # Two reasons this has to happen first. Per-session builtin tools
            # (notably the Discord-native ones) are registered at query build
            # time by resolving session_id -> linked connector session, so a
            # link written only after the first turn left those tools missing on
            # the very first message of a channel. And ``turn_manager.submit``
            # enqueues against an EXISTING session row — it never creates one —
            # so without the row there is nothing to enrol the turn on. On
            # subsequent turns the connector session already carries the link,
            # so this is a no-op.
            ai_session_id = session.get("session_id") if session else None
            if not ai_session_id and session and session.get("id"):
                minted_id = str(uuid.uuid4())
                title = (message.text or "")[:50].strip()
                if len(message.text or "") > 50:
                    title += "..."

                def _mint_blocking() -> None:
                    """The session INSERT and its connector-session link, as
                    ONE hop and ONE unit: a link written without its session
                    row (or vice versa) is exactly the split this must not
                    allow. The sidebar broadcast below stays on the loop — it
                    is a coroutine and would be dropped from a worker thread."""
                    database.create_session(
                        session_id=minted_id,
                        profile_id=profile_id,
                        project_id=project_id,
                        title=title or "Discord conversation",
                        api_user_id=api_user_id,
                        is_active=True,
                        # Run binding from the API user's preconfigured
                        # selection. Permission mode is pinned to Ask so a
                        # profile's bypass/accept-edits setting can never leak
                        # into a headless connector conversation — prompts ride
                        # the connector funnel instead.
                        model=api_user_model,
                        effort=api_user_effort,
                        permission_mode="default",
                    )
                    database.update_connector_session_ai_session(
                        session.get("id"), minted_id
                    )

                try:
                    await run_sync(_mint_blocking)
                    ai_session_id = minted_id
                    # Mirror execute_query's sidebar broadcast for new sessions so
                    # connector-started conversations appear without a refresh.
                    try:
                        from app.core.sync_engine import sync_engine
                        await sync_engine.broadcast_user_session_created(
                            user_id="admin",
                            session_id=minted_id,
                            source_device_id=None,
                        )
                    except Exception:
                        logger.debug("session-created broadcast failed", exc_info=True)
                except Exception:
                    logger.exception(
                        "Failed to pre-create AI session for connector message"
                    )
                    ai_session_id = None

            # No session row means there is nothing to enrol a turn on, so bail
            # rather than falling back to a session-less query (that fallback is
            # exactly the concurrent-loop path this method no longer takes).
            # Nothing to clean up: the typing indicator starts below.
            if not ai_session_id:
                logger.error(
                    f"No AI session for connector {message.connector_id} "
                    f"chat_id={message.platform_chat_id} — dropping the message"
                )
                return

            # Send typing indicator and keep it alive while the turn runs
            typing_task: Optional[asyncio.Task] = None
            if instance:
                # Fire one immediate typing indicator so the user sees it
                # before the query starts (the loop task may not yield in time)
                try:
                    await instance.send_typing_indicator(message.platform_chat_id)
                except Exception:
                    logger.debug("Initial typing indicator failed", exc_info=True)

                async def _keep_typing() -> None:
                    try:
                        while True:
                            await instance.send_typing_indicator(
                                message.platform_chat_id
                            )
                            await asyncio.sleep(4)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.debug("Typing indicator refresh failed", exc_info=True)

                typing_task = asyncio.create_task(_keep_typing())

            # Run the message as a queued turn — import lazily to avoid circular
            # imports.
            from app.core import turn_manager, turn_outcome

            # Build connector context for the system prompt (memory + capabilities)
            overrides: Dict[str, Any] = {}
            if instance and connector_row:
                context_append = self._build_connector_context(
                    connector_row, instance, message.platform_chat_id
                )
                overrides["system_prompt_append"] = context_append

            # Re-assert the API user's run binding every turn (``connector`` is
            # a _BINDING_SOURCES member, so these persist to the session row —
            # editing the API user re-binds existing conversations). The
            # permission mode is ALWAYS Ask: the agent's configured tools list
            # governs, and every non-whitelisted tool raises a permission
            # request that funnels out through the connector.
            if api_user_model:
                overrides["model"] = api_user_model
            if api_user_effort:
                overrides["effort"] = api_user_effort
            overrides["permission_mode"] = "default"

            try:
                # Retain the durable identity: other intake paths can enqueue
                # on this session too, so "latest answer" is never our receipt.
                turn_id = await turn_manager.submit_async(
                    ai_session_id,
                    "connector",
                    prompt=message.text,
                    profile_id=profile_id,
                    project_id=project_id,
                    overrides=overrides or None,
                    # The connector's API user is the turn's principal: its
                    # tool surface and credentials, never the owner's. The
                    # sender only decides whether the owner's subscription
                    # sign-ins may run it (connector_service.sender_is_owner,
                    # checked when the turn starts).
                    payload={
                        "api_user_id": api_user_id,
                        "connector_sender": {
                            "connector_id": message.connector_id,
                            "platform_user_id": message.platform_user_id,
                        },
                    },
                )
                settled = await turn_manager.wait_settled(turn_id, timeout=3600)
            finally:
                if typing_task:
                    typing_task.cancel()

            if settled is None:
                # The turn is durable and keeps running; only our wait gave up,
                # so never interrupt here — just tell the channel.
                logger.error(
                    f"Connector turn did not settle within the wait window for "
                    f"session {ai_session_id}"
                )
                response_text = "I couldn't generate a response."
            else:
                # Both helpers load the session's whole event log — two
                # blocking reads back to back, so they share one hop.
                def _read_outcome() -> Tuple[Dict[str, Any], str]:
                    return (
                        turn_outcome.final_turn_outcome(ai_session_id, turn_id),
                        turn_outcome.extract_final_assistant_text(ai_session_id, turn_id),
                    )

                outcome, final_text = await run_sync(_read_outcome)
                response_text = final_text or "I couldn't generate a response."
                if outcome["error"]:
                    # Still send whatever text the turn produced before it died.
                    logger.error(
                        f"Connector turn failed for session {ai_session_id}: "
                        f"{outcome['error']}"
                    )

            # Send response back through the connector
            sent = False
            if instance:
                sent = await instance.send_message(
                    OutgoingMessage(
                        platform_chat_id=message.platform_chat_id,
                        text=response_text,
                        reply_to_message_id=message.platform_message_id,
                    )
                )
                if not sent:
                    logger.error(
                        f"Failed to send response to platform for connector "
                        f"{message.connector_id} chat_id={message.platform_chat_id}"
                    )

            # Log the inbound message cycle and update last_event_at — two
            # contiguous writes, one hop.
            log_id = secrets.token_hex(8)

            def _log_inbound() -> None:
                database.create_connector_event_log(
                    log_id=log_id,
                    connector_id=message.connector_id,
                    event_type="message.received",
                    direction="inbound",
                    payload={"text": message.text[:100], "platform_chat_id": message.platform_chat_id},
                    status="delivered" if sent else "failed",
                    error=None if sent else "Failed to send response",
                )
                database.update_connector_event_dispatched(message.connector_id, success=sent)

            await run_sync(_log_inbound)

        except Exception as e:
            logger.error(
                f"Error handling incoming message from connector "
                f"{message.connector_id}: {e}",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Permission response handling
    # ------------------------------------------------------------------

    async def handle_permission_response(
        self, request_id: str, session_id: str, approved: bool
    ) -> None:
        """Ignore a tool approval sent by a platform button or callback.

        Tool approvals for connector sessions are the owner's to answer, in the
        app or in a plain-text reply from their own account
        (``connector_permissions.answer_from_chat``). Chats no longer receive
        Approve/Deny buttons, but old ones can still sit in chat history, and
        this callback carries no sender to verify, so every such answer ends
        here and is dropped.
        """
        logger.warning(
            "Ignoring a tool approval sent from a connector chat "
            "(request=%s session=%s approved=%s): only the owner's app or the "
            "owner's own plain-text reply can answer it",
            request_id, session_id, approved,
        )

    # ------------------------------------------------------------------
    # Public accessors (avoids external code reaching into _connectors)
    # ------------------------------------------------------------------

    async def get_running_connector(self, connector_id: str) -> Optional[BaseConnector]:
        """Return a running connector instance by ID, or None."""
        async with self._lock:
            return self._connectors.get(connector_id)

    def get_running_instance(self, connector_id: str) -> Optional[BaseConnector]:
        """Return a running connector instance by ID without acquiring the async
        lock, for synchronous / tool-call resolution paths.

        Reads a single dict key (atomic in CPython); returns None when the
        connector is not running on this install. Lets external code (e.g. the
        Discord-native MCP tools) resolve the live instance without reaching
        into ``_connectors`` directly."""
        return self._connectors.get(connector_id)

    def get_running_connectors(self, connector_type: Optional[str] = None):
        """Yield (connector_id, instance) pairs, optionally filtered by type.

        Note: iterates over a snapshot of the dict to avoid holding the lock.
        """
        snapshot = list(self._connectors.items())
        for cid, instance in snapshot:
            if connector_type is None or instance.connector_type == connector_type:
                yield cid, instance

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def get_connector_status(self, connector_id: str) -> Optional[Dict]:
        """Return runtime status for a running connector, or None."""
        instance = self._connectors.get(connector_id)
        if not instance:
            return None
        return {
            "connector_id": connector_id,
            "status": instance.status.value,
            "status_message": instance.status_message,
        }

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_sensitive_fields(connector_type: str) -> List[str]:
        """Return the list of config keys that contain secrets for each type."""
        from app.core.connector_service import get_sensitive_fields
        return get_sensitive_fields(connector_type)

    @staticmethod
    def _decrypt_config(config: Dict, connector_type: str) -> Dict:
        """Decrypt sensitive fields in a connector config dict."""
        from app.core import encryption

        sensitive = ConnectorManager._get_sensitive_fields(connector_type)
        if not sensitive:
            return config

        decrypted = dict(config)
        for key in sensitive:
            value = decrypted.get(key)
            if value and isinstance(value, str):
                if encryption.is_encrypted(value):
                    if not encryption.is_encryption_ready():
                        logger.warning(
                            f"Cannot decrypt connector config field '{key}' — "
                            "encryption key not loaded"
                        )
                        decrypted[key] = None
                        continue
                    try:
                        decrypted[key] = encryption.decrypt_value(value)
                    except Exception as e:
                        logger.error(f"Failed to decrypt connector field '{key}': {e}")
                        decrypted[key] = None
        return decrypted

    @staticmethod
    def _encrypt_config(config: Dict, connector_type: str) -> Dict:
        """Encrypt sensitive fields in a connector config dict."""
        from app.core import encryption

        sensitive = ConnectorManager._get_sensitive_fields(connector_type)
        if not sensitive:
            return config

        encrypted = dict(config)
        for key in sensitive:
            value = encrypted.get(key)
            if value and isinstance(value, str) and not encryption.is_encrypted(value):
                try:
                    encrypted[key] = encryption.encrypt_value(value)
                except Exception:
                    raise ValueError("Cannot store connector secret: credential storage requires local recovery.") from None
        return encrypted


# Module-level singleton
connector_manager = ConnectorManager()
