import argparse
import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set

import tkinter as tk
from tkinter import ttk

import websockets


PROTOCOL_VERSION = "go2.ctrl.v1"


@dataclass
class ActionSpec:
    key: str
    label: str
    cmd: str
    params_factory: Callable[[], Dict[str, Any]]
    hotkey_hint: str


class RemoteControllerClient:
    def __init__(self, uri: str, ack_timeout: float, ttl_ms: int):
        self.uri = uri
        self.ack_timeout = ack_timeout
        self.ttl_ms = ttl_ms
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _msg_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.uri, max_size=2_000_000)
        await self.send_hello()

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def send_hello(self) -> None:
        hello = {
            "version": PROTOCOL_VERSION,
            "type": "hello",
            "id": self._msg_id("h"),
            "timestamp_ms": self._now_ms(),
            "client": {
                "name": "gui-operator",
                "app": "go2-gui-controller",
                "protocol": PROTOCOL_VERSION,
            },
        }
        await self._send_json(hello)

    async def send_heartbeat(self) -> None:
        heartbeat = {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "id": self._msg_id("hb"),
            "timestamp_ms": self._now_ms(),
        }
        await self._send_json(heartbeat)

    async def send_command(self, cmd: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        command_id = self._msg_id("c")
        payload = {
            "version": PROTOCOL_VERSION,
            "type": "command",
            "id": command_id,
            "timestamp_ms": self._now_ms(),
            "require_ack": True,
            "ttl_ms": self.ttl_ms,
            "cmd": cmd,
            "params": params or {},
        }
        await self._send_json(payload)
        return await self._wait_ack(command_id)

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def _wait_ack(self, message_id: str) -> Dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")

        end_time = time.time() + self.ack_timeout
        while time.time() < end_time:
            remaining = max(0.05, end_time - time.time())
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "ack" and msg.get("id") == message_id:
                return msg

        return {
            "type": "ack",
            "id": message_id,
            "ok": False,
            "code": "TIMEOUT",
            "message": f"No ACK within {self.ack_timeout:.1f}s",
        }


class AsyncWsBridge:
    def __init__(self, uri: str, ack_timeout: float, ttl_ms: int, heartbeat_interval: float):
        self.client = RemoteControllerClient(uri, ack_timeout=ack_timeout, ttl_ms=ttl_ms)
        self.heartbeat_interval = heartbeat_interval
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.heartbeat_task: Optional[asyncio.Task] = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> Future:
        self.thread.start()
        return asyncio.run_coroutine_threadsafe(self._connect_and_heartbeat(), self.loop)

    async def _connect_and_heartbeat(self) -> bool:
        await self.client.connect()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return True

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self.client.send_heartbeat()
            except Exception:
                # Heartbeat failure will be surfaced on next command attempt.
                pass
            await asyncio.sleep(self.heartbeat_interval)

    def send_command(self, cmd: str, params: Optional[Dict[str, Any]] = None) -> Future:
        return asyncio.run_coroutine_threadsafe(self.client.send_command(cmd, params), self.loop)

    def stop(self) -> None:
        shutdown_future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            shutdown_future.result(timeout=3.0)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2.0)

    async def _shutdown(self) -> None:
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass
            self.heartbeat_task = None
        await self.client.close()


class ControlGuiApp:
    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        self.root = root
        self.args = args
        self.uri = f"ws://{args.host}:{args.port}{args.path}"

        self.bridge = AsyncWsBridge(
            uri=self.uri,
            ack_timeout=args.ack_timeout,
            ttl_ms=args.ttl_ms,
            heartbeat_interval=args.heartbeat_interval,
        )
        self.connect_future = self.bridge.start()

        self.actions = self._build_actions()
        self.action_order = list(self.actions.keys())
        self.keymap = {
            "1": "damp",
            "2": "stand_up",
            "3": "stand_down",
            "4": "balance_stand",
            "5": "recovery_stand",
            "space": "stop_move",
            "w": "move_forward",
            "up": "move_forward",
            "s": "move_backward",
            "down": "move_backward",
            "a": "move_left",
            "left": "move_left",
            "d": "move_right",
            "right": "move_right",
            "q": "rotate_left",
            "e": "rotate_right",
        }
        self.pressed_keys: Set[str] = set()

        self.status_var = tk.StringVar(value="Connecting...")
        self.input_state_var = tk.StringVar(value="Input: idle")
        self.last_action_var = tk.StringVar(value="Last action: none")

        self.root.title("Go2 Remote Control GUI")
        self.root.geometry("1040x700")
        self.root.minsize(920, 620)
        self.root.configure(bg="#eef3f6")

        self._build_layout()
        self._bind_keys()
        self._poll_connect_result()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_actions(self) -> Dict[str, ActionSpec]:
        duration = self.args.move_duration_ms
        linear = self.args.linear_speed
        angular = self.args.angular_speed

        return {
            "damp": ActionSpec("damp", "Damp", "damp", lambda: {}, "1"),
            "stand_up": ActionSpec("stand_up", "Stand Up", "stand_up", lambda: {}, "2"),
            "stand_down": ActionSpec("stand_down", "Stand Down", "stand_down", lambda: {}, "3"),
            "balance_stand": ActionSpec("balance_stand", "Balance Stand", "balance_stand", lambda: {}, "4"),
            "recovery_stand": ActionSpec("recovery_stand", "Recovery Stand", "recovery_stand", lambda: {}, "5"),
            "stop_move": ActionSpec("stop_move", "Stop Move", "stop_move", lambda: {}, "Space"),
            "move_forward": ActionSpec(
                "move_forward",
                "Move Forward",
                "move",
                lambda: {"vx": linear, "vy": 0.0, "wz": 0.0, "duration_ms": duration},
                "W / Up",
            ),
            "move_backward": ActionSpec(
                "move_backward",
                "Move Backward",
                "move",
                lambda: {"vx": -linear, "vy": 0.0, "wz": 0.0, "duration_ms": duration},
                "S / Down",
            ),
            "move_left": ActionSpec(
                "move_left",
                "Move Left",
                "move",
                lambda: {"vx": 0.0, "vy": linear, "wz": 0.0, "duration_ms": duration},
                "A / Left",
            ),
            "move_right": ActionSpec(
                "move_right",
                "Move Right",
                "move",
                lambda: {"vx": 0.0, "vy": -linear, "wz": 0.0, "duration_ms": duration},
                "D / Right",
            ),
            "rotate_left": ActionSpec(
                "rotate_left",
                "Rotate Left",
                "move",
                lambda: {"vx": 0.0, "vy": 0.0, "wz": angular, "duration_ms": duration},
                "Q",
            ),
            "rotate_right": ActionSpec(
                "rotate_right",
                "Rotate Right",
                "move",
                lambda: {"vx": 0.0, "vy": 0.0, "wz": -angular, "duration_ms": duration},
                "E",
            ),
        }

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg="#eef3f6", padx=16, pady=16)
        shell.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(shell, bg="#1f4e5f", padx=16, pady=14)
        header.pack(fill=tk.X)

        tk.Label(
            header,
            text="GO2 CONTROL CONSOLE",
            fg="#f5fbff",
            bg="#1f4e5f",
            font=("TkDefaultFont", 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text=f"Endpoint: {self.uri}",
            fg="#d7ebf2",
            bg="#1f4e5f",
            font=("TkDefaultFont", 10),
        ).pack(anchor="w", pady=(3, 0))
        tk.Label(
            header,
            textvariable=self.status_var,
            fg="#ffe7a3",
            bg="#1f4e5f",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(anchor="w", pady=(4, 0))

        main = tk.Frame(shell, bg="#eef3f6")
        main.pack(fill=tk.BOTH, expand=True, pady=(14, 10))
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(0, weight=1)

        left = tk.Frame(main, bg="#ffffff", bd=1, relief=tk.SOLID)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)

        tk.Label(
            left,
            text="Drive Pad (Mouse)",
            bg="#ffffff",
            fg="#124150",
            font=("TkDefaultFont", 12, "bold"),
            padx=10,
            pady=8,
        ).grid(row=0, column=0, sticky="w")

        pad = tk.Frame(left, bg="#ffffff", padx=14, pady=8)
        pad.grid(row=1, column=0, sticky="nsew")
        for idx in range(3):
            pad.columnconfigure(idx, weight=1)

        self._add_action_button(pad, "move_forward", 0, 1, accent="#4c956c")
        self._add_action_button(pad, "move_left", 1, 0, accent="#4c956c")
        self._add_action_button(pad, "stop_move", 1, 1, accent="#d1495b")
        self._add_action_button(pad, "move_right", 1, 2, accent="#4c956c")
        self._add_action_button(pad, "move_backward", 2, 1, accent="#4c956c")
        self._add_action_button(pad, "rotate_left", 3, 0, accent="#2d6a8c")
        self._add_action_button(pad, "rotate_right", 3, 2, accent="#2d6a8c")

        safety = tk.Frame(left, bg="#ffffff", padx=14, pady=10)
        safety.grid(row=2, column=0, sticky="nsew")
        for idx in range(3):
            safety.columnconfigure(idx, weight=1)

        self._add_action_button(safety, "stand_up", 0, 0, accent="#2d6a8c")
        self._add_action_button(safety, "stand_down", 0, 1, accent="#2d6a8c")
        self._add_action_button(safety, "damp", 0, 2, accent="#6d597a")
        self._add_action_button(safety, "balance_stand", 1, 0, accent="#6d597a")
        self._add_action_button(safety, "recovery_stand", 1, 1, accent="#6d597a")

        right = tk.Frame(main, bg="#ffffff", bd=1, relief=tk.SOLID, padx=10, pady=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(5, weight=1)

        tk.Label(
            right,
            text="Action Queue",
            bg="#ffffff",
            fg="#124150",
            font=("TkDefaultFont", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.action_listbox = tk.Listbox(
            right,
            height=9,
            activestyle="dotbox",
            bg="#f8fbfc",
            relief=tk.SOLID,
            bd=1,
            selectbackground="#2d6a8c",
            selectforeground="#ffffff",
        )
        self.action_listbox.grid(row=1, column=0, sticky="nsew", pady=(6, 8))
        for action in self.action_order:
            spec = self.actions[action]
            self.action_listbox.insert(tk.END, f"{spec.label}  [{spec.hotkey_hint}]")
        self.action_listbox.selection_set(0)

        tk.Button(
            right,
            text="Execute Selected (Enter)",
            bg="#2d6a8c",
            fg="#ffffff",
            activebackground="#1f4e5f",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=8,
            pady=8,
            command=self.execute_selected,
        ).grid(row=2, column=0, sticky="ew")

        tk.Label(
            right,
            textvariable=self.input_state_var,
            bg="#ffffff",
            fg="#124150",
            font=("TkDefaultFont", 10, "bold"),
            pady=8,
        ).grid(row=3, column=0, sticky="w")
        tk.Label(
            right,
            textvariable=self.last_action_var,
            bg="#ffffff",
            fg="#47616d",
            font=("TkDefaultFont", 10),
        ).grid(row=4, column=0, sticky="w")

        hints = (
            "Keyboard map: 1/2/3/4/5 safety modes | W/A/S/D or arrows move | Q/E rotate | "
            "Space stop | Enter send selected | Esc quit"
        )
        tk.Label(
            right,
            text=hints,
            bg="#ffffff",
            fg="#4a5f69",
            wraplength=320,
            justify=tk.LEFT,
        ).grid(row=5, column=0, sticky="nw", pady=(8, 4))

        log_frame = tk.Frame(shell, bg="#ffffff", bd=1, relief=tk.SOLID, padx=10, pady=10)
        log_frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            log_frame,
            text="Event Log",
            bg="#ffffff",
            fg="#124150",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w")

        self.log_text = tk.Text(
            log_frame,
            height=10,
            state=tk.DISABLED,
            bg="#f8fbfc",
            fg="#21343d",
            relief=tk.SOLID,
            bd=1,
            font=("TkFixedFont", 10),
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        self._log("WARNING: Ensure no obstacles around robot before issuing movement commands.")

    def _add_action_button(self, parent: tk.Frame, action_key: str, row: int, col: int, accent: str) -> None:
        spec = self.actions[action_key]
        button = tk.Button(
            parent,
            text=spec.label,
            command=lambda key=action_key: self.execute_action(key),
            bg=accent,
            fg="#ffffff",
            activebackground="#1f4e5f",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=6,
            pady=10,
            cursor="hand2",
        )
        button.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")

    def _bind_keys(self) -> None:
        self.root.bind_all("<KeyPress>", self._on_keypress)
        self.root.bind_all("<KeyRelease>", self._on_keyrelease)

    def _poll_connect_result(self) -> None:
        if self.connect_future.done():
            try:
                self.connect_future.result()
                self.status_var.set("Connected")
                self._log("Connected to control server.")
            except Exception as error:
                self.status_var.set("Connection failed")
                self._log(f"Connection failed: {error}")
            return
        self.root.after(120, self._poll_connect_result)

    def _on_keypress(self, event: tk.Event) -> None:
        keysym = str(event.keysym).lower()
        was_pressed = keysym in self.pressed_keys
        self.pressed_keys.add(keysym)
        self._update_input_state()

        if event.widget is self.log_text:
            return

        if keysym in {"return", "kp_enter"}:
            self.execute_selected()
            return
        if keysym == "escape":
            self._on_close()
            return

        if was_pressed:
            return

        action_key = self.keymap.get(keysym)
        if action_key is not None:
            self.execute_action(action_key)

    def _on_keyrelease(self, event: tk.Event) -> None:
        keysym = str(event.keysym).lower()
        if keysym in self.pressed_keys:
            self.pressed_keys.remove(keysym)
        self._update_input_state()

    def _update_input_state(self) -> None:
        if not self.pressed_keys:
            self.input_state_var.set("Input: idle")
            return
        shown = sorted(self.pressed_keys)
        self.input_state_var.set(f"Input: key down -> {', '.join(shown[:4])}")

    def execute_selected(self) -> None:
        selection = self.action_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        action_key = self.action_order[index]
        self.execute_action(action_key)

    def execute_action(self, action_key: str) -> None:
        if action_key not in self.actions:
            return

        spec = self.actions[action_key]
        params = spec.params_factory()
        self.last_action_var.set(f"Last action: {spec.label}")
        self._log(f"Send: cmd={spec.cmd} params={params}")

        try:
            future = self.bridge.send_command(spec.cmd, params)
        except Exception as error:
            self._log(f"Send failed immediately: {error}")
            return

        future.add_done_callback(
            lambda fut, label=spec.label: self.root.after(0, self._handle_ack_result, label, fut)
        )

    def _handle_ack_result(self, action_label: str, future: Future) -> None:
        try:
            ack = future.result()
        except Exception as error:
            self._log(f"ACK error for {action_label}: {error}")
            return

        ok = ack.get("ok")
        code = ack.get("code")
        msg = ack.get("message")
        self._log(f"ACK {action_label}: ok={ok} code={code} msg={msg}")

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        self.status_var.set("Disconnecting...")
        self.bridge.stop()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GUI WebSocket remote control client for Go2")
    parser.add_argument("--host", default="127.0.0.1", help="Robot/server host")
    parser.add_argument("--port", type=int, default=9001, help="WebSocket server port")
    parser.add_argument("--path", default="/control", help="WebSocket path for control")
    parser.add_argument("--ack-timeout", type=float, default=2.0, help="Seconds waiting for ACK")
    parser.add_argument("--ttl-ms", type=int, default=500, help="TTL for command message")
    parser.add_argument("--heartbeat-interval", type=float, default=1.0, help="Heartbeat interval seconds")
    parser.add_argument("--linear-speed", type=float, default=0.3, help="Linear speed for move commands")
    parser.add_argument("--angular-speed", type=float, default=0.6, help="Angular speed for rotate commands")
    parser.add_argument("--move-duration-ms", type=int, default=800, help="Duration for each move command")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = tk.Tk()
    ControlGuiApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()