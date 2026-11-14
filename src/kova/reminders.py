"""Reminder loop: fire due reminders and scheduled jobs, push events.

One loop drives both: a due reminder publishes a ``reminder`` event and shows a
desktop toast, and a due job is handed to :class:`kova.scheduler.Scheduler`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime

from kova.events import EventBus
from kova.scheduler import Scheduler
from kova.store import Store

log = logging.getLogger(__name__)

APP_NAME = "Kova"

# Windows toasts need an AppUserModelID the system already knows. PowerShell's
# own id is registered on every Windows install, so scripts may toast with it.
_WINDOWS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_APPLESCRIPT_ESCAPES = str.maketrans({"\\": "\\\\", '"': '\\"'})


def _ps_quote(value: str) -> str:
    """Quote one PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def _windows_toast(text: str) -> list[str]:
    """PowerShell toast through ``Windows.UI.Notifications``."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime]|Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode({_ps_quote(APP_NAME)}))|Out-Null;"
        f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode({_ps_quote(text)}))|Out-Null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"{_ps_quote(_WINDOWS_APP_ID)}).Show([Windows.UI.Notifications.ToastNotification]::new($t));"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def notification_plan(
    text: str,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[tuple[str, list[str]]]:
    """Return the notifier name and command to try, best first, for ``platform``.

    An empty list means this system has no notifier Kova knows how to call.
    ``platform``, ``which`` and the result are plain data so tests can pin a
    platform without a matching machine.
    """
    platform = platform or sys.platform
    which = which or shutil.which
    plan: list[tuple[str, list[str]]] = []
    if platform.startswith("linux") or "bsd" in platform:
        if which("notify-send"):
            plan.append(("notify-send", ["notify-send", APP_NAME, text]))
        if which("gdbus"):
            plan.append(
                (
                    "gdbus",
                    [
                        "gdbus",
                        "call",
                        "--session",
                        "--dest",
                        "org.freedesktop.Notifications",
                        "--object-path",
                        "/org/freedesktop/Notifications",
                        "--method",
                        "org.freedesktop.Notifications.Notify",
                        APP_NAME,
                        "0",
                        "",
                        APP_NAME,
                        text,
                        "[]",
                        "{}",
                        "5000",
                    ],
                )
            )
    elif platform == "darwin":
        if which("osascript"):
            body = text.replace("\r", " ").replace("\n", " ").translate(_APPLESCRIPT_ESCAPES)
            script = f'display notification "{body}" with title "{APP_NAME}"'
            plan.append(("osascript", ["osascript", "-e", script]))
    elif platform.startswith("win"):
        for shell in ("powershell", "pwsh"):
            if which(shell):
                plan.append((shell, _windows_toast(text)))
                break
        if which("msg.exe") or which("msg"):
            plan.append(("msg.exe", ["msg", "*", f"{APP_NAME}: {text}"]))
    return plan


def desktop_notify(text: str) -> None:
    """Show a desktop notification with this platform's notifier.

    Tries each notifier in :func:`notification_plan` in turn. Never raises: a
    missing binary, a failed launch or an unsupported platform is ignored, so
    the reminder loop keeps running and client windows still show the reminder.
    """
    try:
        for name, command in notification_plan(text):
            try:
                subprocess.Popen(command)
                return
            except Exception:
                log.debug("desktop notifier %s failed", name, exc_info=True)
    except Exception:  # pragma: no cover - defensive, never raises
        log.exception("desktop notification failed")


class ReminderLoop:
    """Fire due reminders and due jobs on an interval, one notification each.

    ``scheduler`` is optional: without it this loop only fires one-shot
    reminders, exactly as before jobs existed.
    """

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        now: Callable[[], datetime],
        notify: Callable[[str], None] = desktop_notify,
        interval: float = 15,
        scheduler: Scheduler | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.now = now
        self.notify = notify
        self.interval = interval
        self.scheduler = scheduler

    def tick(self) -> list[dict]:
        """Fire every reminder due now, once, and return the events published."""
        fired: list[dict] = []
        for reminder in self.store.due_reminders(self.now()):
            event = {"type": "reminder", "id": reminder["id"], "text": reminder["text"]}
            self.bus.publish(event)
            try:
                self.notify(reminder["text"])
            except Exception:
                log.exception("desktop notification failed")
            self.store.mark_reminder_done(reminder["id"])
            fired.append(event)
        return fired

    async def run(self) -> None:
        """Tick on the interval until the task is cancelled."""
        try:
            while True:
                try:
                    await asyncio.sleep(self.interval)
                    self.tick()
                    if self.scheduler is not None:
                        await self.scheduler.due(self.now())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("reminder loop tick failed")
        finally:
            if self.scheduler is not None:
                await self.scheduler.shutdown()
