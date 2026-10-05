#!/usr/bin/env python3
"""Bridge one Windows WeChat contact to Codex over JSON Lines.

This process does not call an LLM. It listens to one contact, publishes text
messages on stdout, waits for a command on stdin, and sends that command via
wxauto.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, Optional


EVENT_PREFIX = "WECHAT_SKILL "


def configure_io() -> None:
    """Use UTF-8 for the Codex protocol on Windows consoles and PTYs."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


@contextlib.contextmanager
def quiet_stdout() -> Iterator[io.StringIO]:
    """Keep wxauto initialization output out of the machine protocol."""
    captured = io.StringIO()
    previous = sys.stdout
    sys.stdout = captured
    try:
        yield captured
    finally:
        sys.stdout = previous


class Bridge:
    def __init__(self, args: argparse.Namespace, ui_enabled: bool) -> None:
        self.args = args
        self.ui_enabled = ui_enabled
        self.stop_event = threading.Event()
        self.commands: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.ui_events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.wx: Any = None
        self.contact = args.contact.strip()
        self.message_counter = 0
        self.seen_runtime_ids: set[str] = set()
        self.recent_message_fingerprints: Dict[str, float] = {}
        self.pending_order: Deque[str] = deque()
        self.pending_decisions: Dict[str, Dict[str, Any]] = {}
        self.completed_ids: set[str] = set()
        self._queue_lock = threading.Lock()
        self._closed = False

    def emit(self, event: str, **payload: Any) -> None:
        item = {"event": event, "time": time.time(), **payload}
        try:
            print(EVENT_PREFIX + json.dumps(item, ensure_ascii=False), flush=True)
        except (OSError, ValueError):
            pass
        if self.ui_enabled:
            with self._queue_lock:
                # Keep the terminal protocol observable, but do not allow
                # background readers to repopulate queues after shutdown.
                if not self._closed or event == "stopped":
                    self.ui_events.put(item)

    def request_stop(self, reason: str = "user") -> None:
        if not self.stop_event.is_set():
            self.emit("stopping", reason=reason)
        self.stop_event.set()

    @staticmethod
    def _drain_queue(items: "queue.Queue[Any]") -> None:
        """Remove all currently buffered items without waiting on producers."""
        while True:
            try:
                items.get_nowait()
            except queue.Empty:
                return

    def clear_queues(self) -> None:
        """Discard every pending command, reply, and UI event at shutdown."""
        with self._queue_lock:
            self._closed = True
            self._drain_queue(self.commands)
            self._drain_queue(self.ui_events)
            self.pending_order.clear()
            self.pending_decisions.clear()
            self.completed_ids.clear()
            self.seen_runtime_ids.clear()
            self.recent_message_fingerprints.clear()

    def prime_listener_baseline(self) -> None:
        """Mark messages already visible at startup as historical messages."""
        listeners = getattr(self.wx, "listen", {})
        chat = listeners.get(self.contact) if isinstance(listeners, dict) else None
        if chat is None:
            raise RuntimeError("无法初始化微信监听窗口")

        messages = chat.GetAllMessage(
            savepic=chat.savepic,
            savefile=chat.savefile,
            savevoice=chat.savevoice,
        )
        baseline_ids = []
        for message in messages or []:
            runtime_id = getattr(message, "id", None)
            if runtime_id is None:
                try:
                    runtime_id = message[-1]
                except (IndexError, KeyError, TypeError):
                    runtime_id = None
            if runtime_id is not None:
                baseline_ids.append(str(runtime_id))

        # wxauto treats an empty usedmsgid list as "not initialized" and
        # would discard the first real message. A sentinel keeps an empty
        # chat initialized while remaining different from every runtime id.
        chat.usedmsgid = baseline_ids or ["__codex_wechat_empty_baseline__"]
        self.seen_runtime_ids.update(baseline_ids)

    def load_wxauto(self) -> tuple[Any, Any]:
        vendor = Path(__file__).resolve().parents[1] / "vendor"
        package = vendor / "wxauto"
        if not (package / "__init__.py").is_file():
            raise RuntimeError("找不到内置 wxauto，请确认 vendor/wxauto/ 完整存在。")
        sys.path.insert(0, str(vendor))
        import wxauto  # type: ignore
        import wxauto.uiautomation as uia  # type: ignore

        return wxauto, uia

    def read_commands(self) -> None:
        """Read Codex commands without blocking the wxauto worker."""
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    command = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.emit("warning", message=f"无法解析 Codex 命令: {exc}")
                    continue
                if isinstance(command, dict):
                    with self._queue_lock:
                        if not self._closed:
                            self.commands.put(command)
        except (OSError, ValueError):
            pass
        finally:
            self.request_stop("stdin_closed")

    def process_commands(self) -> None:
        """Accept replies without pausing the WeChat polling loop."""
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return

            action = command.get("action")
            command_id = str(command.get("id", ""))
            if action == "stop":
                self.request_stop("codex")
                return
            if action == "ping":
                self.emit("pong")
                continue
            if action not in {"reply", "skip"}:
                self.emit("warning", message="忽略了未知的 Codex 命令")
                continue
            if command_id not in self.pending_order:
                if command_id in self.completed_ids:
                    self.emit("warning", message="忽略了已处理消息的重复回复")
                else:
                    self.emit("warning", message="忽略了与当前消息不匹配的回复")
                continue
            if command_id in self.pending_decisions:
                self.emit("warning", message="忽略了同一消息的重复回复")
                continue
            if action == "skip":
                self.pending_decisions[command_id] = {"skip": True}
                continue
            text = command.get("text")
            self.pending_decisions[command_id] = (
                {"text": text.strip()} if isinstance(text, str) else {"skip": True}
            )

    def flush_replies(self) -> None:
        """Send ready replies in original incoming-message order."""
        while self.pending_order and not self.stop_event.is_set():
            message_id = self.pending_order[0]
            decision = self.pending_decisions.get(message_id)
            if decision is None:
                return
            self.pending_order.popleft()
            self.pending_decisions.pop(message_id, None)
            self.completed_ids.add(message_id)

            if decision.get("skip"):
                self.emit("skipped", id=message_id, reason="codex_skipped")
                continue

            reply = str(decision.get("text", "")).strip()
            if not reply:
                self.emit("skipped", id=message_id, reason="empty_reply")
                continue
            try:
                self.wx.SendMsg(reply, who=self.contact)
            except Exception as exc:  # noqa: BLE001 - surface UI errors.
                self.emit("send_failed", id=message_id, text=reply, message=str(exc))
                continue
            self.emit("sent", id=message_id, contact=self.contact, text=reply)
            self.emit("display", direction="outgoing", id=message_id, text=reply)

    @staticmethod
    def iter_messages(raw: Any) -> Iterator[Any]:
        if isinstance(raw, dict):
            for value in raw.values():
                yield from Bridge.iter_messages(value)
        elif isinstance(raw, (list, tuple)):
            yield from raw
        elif raw is not None:
            yield raw

    @staticmethod
    def text_content(message: Any) -> Optional[str]:
        # wxauto represents images, files, voice, and other media as markers.
        if getattr(message, "type", None) != "friend":
            return None
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text or text.startswith(("[", "【")):
            return None
        return text

    def is_new_message(self, message: Any, text: str) -> bool:
        """Deduplicate stable wxauto ids without dropping real repeated text."""
        now = time.monotonic()
        runtime_id = getattr(message, "id", None)
        if runtime_id is not None:
            value = str(runtime_id)
            if value in self.seen_runtime_ids:
                return False
            self.seen_runtime_ids.add(value)
            if len(self.seen_runtime_ids) > 1000:
                self.seen_runtime_ids.clear()
            return True

        # Some wxauto/message test doubles may omit id. This fallback only
        # protects against a tight-loop duplicate and still allows repeated
        # text after a short interval.
        sender = str(getattr(message, "sender", self.contact))
        fingerprint = f"{sender}\x00{text}"
        last_seen = self.recent_message_fingerprints.get(fingerprint)
        if last_seen is not None and now - last_seen < 2:
            return False
        self.recent_message_fingerprints[fingerprint] = now

        cutoff = now - 2
        self.recent_message_fingerprints = {
            key: value
            for key, value in self.recent_message_fingerprints.items()
            if value >= cutoff
        }
        return True

    def run_worker(self) -> None:
        uia = None
        try:
            wxauto, uia = self.load_wxauto()
            uia.InitializeUIAutomationInCurrentThread()
            with quiet_stdout():
                self.wx = wxauto.WeChat()
                resolved = self.wx.ChatWith(self.contact, timeout=3)
                if not resolved:
                    raise RuntimeError(f"未找到联系人：{self.contact}")
                resolved = str(resolved).strip()
                self.wx.AddListenChat(resolved)
            self.contact = resolved
            self.prime_listener_baseline()
            self.emit(
                "ready",
                contact=self.contact,
                requires_active_polling=True,
                poll_timeout_seconds=30,
                background=self.args.background,
                persona=self.args.persona,
                purpose=self.args.purpose,
                style=self.args.style,
                limits=self.args.limits,
            )

            while not self.stop_event.is_set():
                raw = self.wx.GetListenMessage(self.contact)
                for message in self.iter_messages(raw):
                    if self.stop_event.is_set():
                        break
                    text = self.text_content(message)
                    if text is None or not self.is_new_message(message, text):
                        continue

                    self.message_counter += 1
                    message_id = f"m{self.message_counter}-{time.time_ns()}"
                    self.emit(
                        "message",
                        id=message_id,
                        contact=self.contact,
                        sender=getattr(message, "sender", self.contact),
                        text=text,
                    )
                    self.emit("display", direction="incoming", id=message_id, text=text)
                    self.pending_order.append(message_id)
                self.process_commands()
                self.flush_replies()
                self.stop_event.wait(max(0.02, self.args.poll_interval))
        except Exception as exc:  # noqa: BLE001 - surface startup/runtime errors.
            self.emit("error", message=str(exc))
            self.request_stop("error")
        finally:
            try:
                if self.wx is not None:
                    try:
                        self.wx.StopListening(remove=False)
                    except Exception:
                        pass
                if uia is not None:
                    try:
                        uia.UninitializeUIAutomationInCurrentThread()
                    except Exception:
                        pass
            finally:
                self.clear_queues()
                self.emit("stopped", reason="listener_stopped")

    def start(self) -> None:
        self.worker = threading.Thread(target=self.run_worker, name="wxauto-listener", daemon=True)
        self.worker.start()
        threading.Thread(target=self.read_commands, name="codex-command-reader", daemon=True).start()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信到 Codex 的 wxauto 消息桥接器")
    parser.add_argument("--contact", required=True, help="要监听的微信联系人昵称或备注")
    parser.add_argument("--background", default="", help="聊天背景或双方认识情境")
    parser.add_argument("--persona", default="", help="用户在聊天中扮演的人设")
    parser.add_argument("--purpose", default="自然聊天", help="本次聊天目的")
    parser.add_argument(
        "--style",
        default="自然、像朋友、口语化、简短、不反问、不解释自己是 AI",
        help="聊天语气和风格",
    )
    parser.add_argument("--limits", default="", help="用户明确禁止的话题或行为")
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--no-gui", action="store_true", help="不显示停止窗口")
    return parser.parse_args()


def run_gui(bridge: Bridge) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("Codex 微信聊天")
    root.geometry("430x220")
    root.protocol("WM_DELETE_WINDOW", lambda: bridge.request_stop("window_closed"))
    tk.Label(root, text=f"监听联系人：{bridge.contact}", anchor="w").pack(fill="x", padx=16, pady=(16, 4))
    status = tk.StringVar(value="正在连接微信…")
    tk.Label(root, textvariable=status, anchor="w", fg="#555").pack(fill="x", padx=16, pady=4)
    tk.Label(root, text="Codex 会读取新文本消息并生成回复。点击下方按钮即可停止。", anchor="w").pack(fill="x", padx=16, pady=4)
    stop_button = tk.Button(root, text="停止监听", width=14, command=lambda: bridge.request_stop("user"))
    stop_button.pack(pady=12)

    def poll_ui() -> None:
        try:
            while True:
                event = bridge.ui_events.get_nowait()
                kind = event["event"]
                if kind == "ready":
                    status.set("监听中，等待新消息…")
                elif kind == "message":
                    status.set("收到新消息，等待 Codex 回复…")
                elif kind == "sent":
                    status.set("已发送回复，继续监听…")
                elif kind == "send_failed":
                    status.set("回复发送失败，继续监听…")
                elif kind == "error":
                    status.set("错误：" + str(event.get("message", "未知错误")))
                elif kind == "stopped":
                    status.set("已停止")
                    stop_button.config(state=tk.DISABLED)
                    root.after(500, root.destroy)
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(100, poll_ui)

    poll_ui()
    root.mainloop()


def main() -> None:
    configure_io()
    args = parse_args()
    if sys.platform != "win32":
        print(EVENT_PREFIX + json.dumps({"event": "error", "message": "该 skill 仅支持 Windows。"}, ensure_ascii=False), flush=True)
        return
    bridge = Bridge(args, ui_enabled=not args.no_gui)
    bridge.start()
    try:
        if args.no_gui:
            while not bridge.stop_event.is_set():
                time.sleep(0.25)
        else:
            run_gui(bridge)
    except KeyboardInterrupt:
        bridge.request_stop("keyboard_interrupt")
    except Exception as exc:  # Tk may be unavailable in a headless session.
        bridge.emit("warning", message=f"无法显示停止窗口：{exc}")
        while not bridge.stop_event.is_set():
            time.sleep(0.25)
    try:
        if bridge.worker is not None:
            bridge.worker.join(timeout=20)
    finally:
        # The worker normally performs this cleanup too. Keeping a final
        # process-level cleanup covers GUI exits and startup failures.
        bridge.clear_queues()


if __name__ == "__main__":
    main()
