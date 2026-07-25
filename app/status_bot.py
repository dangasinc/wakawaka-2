"""
StatusBot wires up a neonize (whatsmeow) WhatsApp client that:
  - logs in via a phone-number pairing code (no QR scanning required)
  - detects incoming WhatsApp Status updates from your contacts
  - marks them as viewed
  - reacts to them with an emoji ("likes" them)

Notes on how status detection works:
WhatsApp statuses arrive over the same protocol channel as normal messages,
addressed to the special JID "status@broadcast", with the actual poster in
the message's sender field. There is no separate "status event" in the
underlying protocol — we simply filter the normal message stream for that
chat JID.
"""
from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from collections import deque
from typing import Deque, Set

from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    event,
)
from neonize.utils.enum import ReceiptType
from neonize.utils.jid import JIDToNonAD, build_jid

from . import health_server
from .config import Config

logger = logging.getLogger("status_bot")

STATUS_BROADCAST_JID = build_jid("status", server="broadcast")


def _is_status_broadcast(chat) -> bool:
    return chat.User == STATUS_BROADCAST_JID.User and chat.Server == STATUS_BROADCAST_JID.Server


class _SeenIds:
    """Bounded set that remembers recently processed message IDs so a
    reconnect / offline-sync replay doesn't cause double reactions."""

    def __init__(self, max_size: int = 5000) -> None:
        self._order: Deque[str] = deque()
        self._set: Set[str] = set()
        self._max_size = max_size
        self._lock = threading.Lock()

    def add_if_new(self, item: str) -> bool:
        with self._lock:
            if item in self._set:
                return False
            self._order.append(item)
            self._set.add(item)
            if len(self._order) > self._max_size:
                oldest = self._order.popleft()
                self._set.discard(oldest)
            return True


def _prompt_for_phone_number() -> str:
    print()
    print("=" * 64)
    print("No existing WhatsApp session found — pairing is required.")
    print("=" * 64)
    while True:
        raw = input(
            "Enter the WhatsApp number to pair "
            "(digits only, country code, no '+' or spaces, e.g. 15551234567): "
        ).strip()
        digits = raw.lstrip("+").replace(" ", "").replace("-", "")
        if digits.isdigit() and len(digits) >= 8:
            return digits
        print("That doesn't look like a valid number — try again.")


class StatusBot:
    def __init__(self, config: type[Config]) -> None:
        self.config = config
        os.makedirs(os.path.dirname(config.SESSION_DB_PATH) or ".", exist_ok=True)

        # Log exactly what's being used, before anything else — this is the
        # single most common source of "it still asks me to pair" reports:
        # SESSION_DB_PATH pointing somewhere different from wherever a
        # session file was actually placed (e.g. a Railway Volume mount).
        exists = os.path.exists(config.SESSION_DB_PATH)
        size = os.path.getsize(config.SESSION_DB_PATH) if exists else 0
        logger.info("Session file path: %s", config.SESSION_DB_PATH)
        logger.info(
            "Session file found: %s%s",
            exists,
            f" ({size} bytes)" if exists else " — a fresh pairing will be required",
        )

        self.client = NewClient(config.SESSION_DB_PATH)
        logger.info("is_logged_in (from existing session, if any): %s", self.client.is_logged_in)

        self._seen = _SeenIds()
        self._viewed_count = 0
        self._liked_count = 0
        self._counters_lock = threading.Lock()
        self._startup_notified = False
        self._register_handlers()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        client = self.client

        @client.qr
        def _suppress_qr_terminal_spam(_client, _data: bytes) -> None:
            # neonize registers a default handler that prints an ASCII QR
            # code straight to the terminal (via segno) any time whatsmeow's
            # normal QR login channel fires — which it does in the
            # background even when we're using PairPhone/pairing-code login
            # instead. Left at its default, this repeatedly dumps garbled,
            # line-wrapped QR blocks into any non-interactive log viewer
            # (like Railway's), burying the actual pairing code. We only
            # want the pairing-code flow, so this override is a deliberate
            # no-op.
            logger.debug("Ignoring a QR event (this app uses pairing-code login only)")

        @client.event(ConnectedEv)
        def on_connected(_client, _ev) -> None:
            logger.info("Connected to WhatsApp")
            health_server.state.update(connected=True)
            # ConnectedEv fires once the client is genuinely authenticated
            # and ready to send — true for both a fresh pairing (after the
            # code is entered) and a resumed existing session. Guarded so
            # it only fires once per run, not on every reconnect.
            if self.config.NOTIFY_ON_STARTUP and not self._startup_notified:
                self._startup_notified = True
                threading.Thread(target=self._send_startup_notification, daemon=True).start()

        @client.event(DisconnectedEv)
        def on_disconnected(_client, _ev) -> None:
            logger.warning("Disconnected from WhatsApp")
            health_server.state.update(connected=False)

        @client.event(LoggedOutEv)
        def on_logged_out(_client, _ev) -> None:
            logger.error(
                "This device was logged out by WhatsApp (unlinked from the "
                "phone). Delete %s and redeploy to pair again.",
                self.config.SESSION_DB_PATH,
            )
            health_server.state.update(connected=False, logged_in=False)

        @client.event(PairStatusEv)
        def on_pair_status(_client, ev) -> None:
            logger.info("Paired successfully as %s", ev.ID.User)
            logger.info("✅ Bot connected successfully — now watching for statuses.")
            health_server.state.update(logged_in=True, pairing_code=None)

        @client.event(MessageEv)
        def on_message(client, ev) -> None:
            try:
                self._handle_message(client, ev)
            except Exception:
                logger.exception("Error while handling an incoming event")

    def _handle_message(self, client, ev) -> None:
        source = ev.Info.MessageSource

        if not _is_status_broadcast(source.Chat):
            return  # not a status update, ignore
        if source.IsFromMe:
            return  # don't react to your own posted statuses

        if self.config.ALLOWED_STATUS_SENDERS:
            if source.Sender.User not in self.config.ALLOWED_STATUS_SENDERS:
                return

        message_id = ev.Info.ID
        if not self._seen.add_if_new(message_id):
            return  # already processed (e.g. replayed on reconnect)

        logger.info("New status from %s (id=%s)", source.Sender.User, message_id)

        # Do the actual work off the event-dispatch thread so a slow network
        # call (or the intentional human-like delay before reacting) never
        # blocks processing of the next incoming event.
        threading.Thread(
            target=self._process_status,
            args=(client, source, message_id),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Status actions
    # ------------------------------------------------------------------
    def _process_status(self, client, source, message_id: str) -> None:
        if self.config.VIEW_STATUSES:
            self._view_status(client, source, message_id)
        if self.config.LIKE_STATUSES:
            self._like_status(client, source, message_id)

    def _view_status(self, client, source, message_id: str) -> None:
        try:
            client.mark_read(
                message_id,
                chat=source.Chat,
                sender=source.Sender,
                receipt=ReceiptType.READ,
            )
            with self._counters_lock:
                self._viewed_count += 1
                health_server.state.update(statuses_viewed=self._viewed_count)
            logger.debug("Marked status %s as viewed", message_id)
        except Exception:
            logger.exception("Failed to mark status %s as viewed", message_id)

    def _like_status(self, client, source, message_id: str) -> None:
        delay = random.uniform(
            self.config.MIN_REACT_DELAY_SECONDS, self.config.MAX_REACT_DELAY_SECONDS
        )
        time.sleep(delay)
        try:
            # Verified against whatsmeow's own godoc example and against the
            # actual source of a real, actively maintained whatsmeow-based
            # project (go-whatsapp-web-multidevice): `to` must be the SAME
            # JID as build_reaction's `chat` argument — never the poster's
            # JID directly. For a status this means `to = status@broadcast`.
            #
            # Sending anything to status@broadcast has a known, still-open
            # whatsmeow issue where the server can reject it with a
            # "participant list hash" mismatch (#668 on the whatsmeow
            # tracker). Refreshing the status privacy list first is a
            # best-effort nudge to get that cache in sync before sending;
            # it's not guaranteed to eliminate the error, since this looks
            # like a rough edge in whatsmeow itself rather than something
            # fixable purely from calling code.
            try:
                client.get_status_privacy()
            except Exception:
                logger.debug("get_status_privacy() warm-up failed, continuing anyway")

            reaction_message = client.build_reaction(
                source.Chat, source.Sender, message_id, reaction=self.config.REACTION_EMOJI
            )
            client.send_message(source.Chat, reaction_message)
            with self._counters_lock:
                self._liked_count += 1
                health_server.state.update(statuses_liked=self._liked_count)
            logger.info("Reacted to status %s with %s", message_id, self.config.REACTION_EMOJI)
        except Exception:
            logger.exception("Failed to react to status %s", message_id)

    def _send_startup_notification(self) -> None:
        # Small buffer so the freshly authenticated session has settled
        # before we try to send through it.
        time.sleep(2)
        try:
            me = self.client.get_me()
            own_jid = JIDToNonAD(me.JID)
            self.client.send_message(own_jid, self.config.STARTUP_NOTIFICATION_MESSAGE)
            logger.info("Sent startup confirmation message to your own WhatsApp inbox")
        except Exception:
            logger.exception("Failed to send startup notification to your own inbox")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def run(self, restart_after_seconds: float | None = None) -> None:
        # event is a module-level singleton reused by neonize; clear it up
        # front so a stale "set" state can't linger from a previous call.
        # Doing this before connecting (rather than right before wait())
        # matters: if shutdown() is triggered while still pairing/connecting,
        # we want that signal to still be there when we reach wait() below.
        event.clear()

        already_paired = self.client.is_logged_in

        if not already_paired and not self.config.PHONE_NUMBER:
            if sys.stdin.isatty():
                self.config.PHONE_NUMBER = _prompt_for_phone_number()
            else:
                raise SystemExit(
                    "No existing session and PHONE_NUMBER is not set. In a "
                    "non-interactive environment (e.g. Railway), set the "
                    "PHONE_NUMBER environment variable before starting the app."
                )

        logger.info("Connecting to WhatsApp servers...")
        self.client.connect()

        if not already_paired:
            # whatsmeow recommends requesting the pairing code immediately
            # after the websocket connects; give it a brief moment first.
            time.sleep(2)
            try:
                code = self.client.PairPhone(self.config.PHONE_NUMBER, True)
            except Exception:
                logger.exception("Failed to request a pairing code")
                raise
            health_server.state.update(pairing_code=code)
            logger.info("=" * 64)
            logger.info("WHATSAPP PAIRING CODE: %s", code)
            logger.info(
                "On your phone: WhatsApp > Settings > Linked Devices > "
                "Link a Device > 'Link with phone number instead' > enter this code"
            )
            logger.info("=" * 64)
        else:
            logger.info("Existing session found — resuming without a new pairing code")
            logger.info("✅ Bot connected successfully — now watching for statuses.")

        if restart_after_seconds:
            finished_cleanly = event.wait(timeout=restart_after_seconds)
            if finished_cleanly:
                return  # a real shutdown() happened — nothing more to do
            logger.info(
                "Scheduled restart interval (%.1fh) reached — restarting the "
                "process for a clean session. Pairing will be skipped since "
                "the session is already saved.",
                restart_after_seconds / 3600,
            )
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Error while disconnecting for scheduled restart")
            # Full process re-exec (same technique used for the dependency
            # bootstrap) rather than constructing a new client in-process —
            # this guarantees a completely clean Go/ctypes runtime state
            # instead of relying on the underlying library supporting a
            # live disconnect-then-reconnect cycle within one process.
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            event.wait()  # blocks until shutdown() calls event.set()

    def shutdown(self) -> None:
        logger.info("Shutting down WhatsApp client...")
        try:
            self.client.disconnect()
        except Exception:
            logger.exception("Error while disconnecting")
        event.set()
