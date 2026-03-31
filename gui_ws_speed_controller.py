import argparse
import asyncio
import json
import sys
import time
import uuid
from concurrent.futures import Future
from typing import Any, Dict, Optional

import websockets
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PROTOCOL_VERSION = "go2.ctrl.v1"


def _is_spinbox_or_child(widget: Optional[QWidget]) -> bool:
    current = widget
    while current is not None:
        if isinstance(current, QAbstractSpinBox):
            return True
        current = current.parentWidget()
    return False


class FocusClearWidget(QWidget):
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        clicked = self.childAt(event.pos())
        if clicked is None:
            focused = QApplication.focusWidget()
            if _is_spinbox_or_child(focused):
                focused.clearFocus()
                self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)


class RemoteControllerClient:
    def __init__(self, uri: str, ack_timeout: float, ttl_ms: int):
        self.uri = uri
        self.ack_timeout = ack_timeout
        self.ttl_ms = ttl_ms
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_acks: Dict[str, asyncio.Future] = {}

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _msg_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.uri, max_size=2_000_000)
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self.send_hello(wait_ack=False)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self.ws is not None:
            await self.ws.close()
            self.ws = None

        for fut in self._pending_acks.values():
            if not fut.done():
                fut.cancel()
        self._pending_acks.clear()

    async def send_hello(self, wait_ack: bool = False) -> Dict[str, Any]:
        msg_id = self._msg_id("h")
        payload = {
            "version": PROTOCOL_VERSION,
            "type": "hello",
            "id": msg_id,
            "timestamp_ms": self._now_ms(),
            "client": {
                "name": "gui-operator",
                "app": "go2-gui-speed-controller",
                "protocol": PROTOCOL_VERSION,
            },
        }
        await self._send_json(payload)
        if wait_ack:
            return await self._wait_ack(msg_id)
        return {"ok": True, "code": "SENT", "id": msg_id}

    async def send_heartbeat(self) -> None:
        payload = {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "id": self._msg_id("hb"),
            "timestamp_ms": self._now_ms(),
        }
        await self._send_json(payload)

    async def send_command(
        self,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
        wait_ack: bool = False,
    ) -> Dict[str, Any]:
        msg_id = self._msg_id("c")
        payload = {
            "version": PROTOCOL_VERSION,
            "type": "command",
            "id": msg_id,
            "timestamp_ms": self._now_ms(),
            "require_ack": True,
            "ttl_ms": self.ttl_ms,
            "cmd": cmd,
            "params": params or {},
        }
        await self._send_json(payload)
        if wait_ack:
            return await self._wait_ack(msg_id)
        return {"ok": True, "code": "SENT", "id": msg_id}

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def _wait_ack(self, message_id: str) -> Dict[str, Any]:
        future = asyncio.get_running_loop().create_future()
        self._pending_acks[message_id] = future
        try:
            return await asyncio.wait_for(future, timeout=self.ack_timeout)
        except asyncio.TimeoutError:
            return {
                "type": "ack",
                "id": message_id,
                "ok": False,
                "code": "TIMEOUT",
                "message": f"No ACK within {self.ack_timeout:.1f}s",
            }
        finally:
            self._pending_acks.pop(message_id, None)

    async def _reader_loop(self) -> None:
        if self.ws is None:
            return
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") == "ack":
                    msg_id = str(msg.get("id", ""))
                    fut = self._pending_acks.get(msg_id)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif msg.get("type") == "event":
                    print(
                        f"[event] level={msg.get('level')} event={msg.get('event')} detail={msg.get('detail')}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[reader] stopped: {error}")


class AsyncWsBridge:
    def __init__(self, uri: str, ack_timeout: float, ttl_ms: int, heartbeat_interval: float):
        self.client = RemoteControllerClient(uri, ack_timeout=ack_timeout, ttl_ms=ttl_ms)
        self.heartbeat_interval = heartbeat_interval
        self.loop = asyncio.new_event_loop()
        self._thread = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> Future:
        import threading

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return asyncio.run_coroutine_threadsafe(self._connect_and_start_heartbeat(), self.loop)

    async def _connect_and_start_heartbeat(self) -> bool:
        await self.client.connect()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return True

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self.client.send_heartbeat()
            except Exception:
                pass
            await asyncio.sleep(self.heartbeat_interval)

    def send_command(self, cmd: str, params: Optional[Dict[str, Any]] = None, wait_ack: bool = False) -> Future:
        return asyncio.run_coroutine_threadsafe(
            self.client.send_command(cmd, params=params, wait_ack=wait_ack),
            self.loop,
        )

    def stop(self) -> None:
        shutdown_future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            shutdown_future.result(timeout=3.0)
        except Exception:
            pass

        self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    async def _shutdown(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        await self.client.close()


class SpeedControlWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self.uri = f"ws://{args.host}:{args.port}{args.path}"
        self.setWindowTitle("Go2 GUI Speed Controller")
        self.setMinimumSize(900, 560)

        self.bridge = AsyncWsBridge(
            uri=self.uri,
            ack_timeout=args.ack_timeout,
            ttl_ms=args.ttl_ms,
            heartbeat_interval=args.heartbeat_interval,
        )
        self.connect_future = self.bridge.start()

        self.key_press_time = {k: 0.0 for k in ["w", "s", "a", "d", "q", "e"]}
        self.key_pressed = {k: False for k in ["w", "s", "a", "d", "q", "e"]}
        self.last_velocity = {"vx": 0.0, "vy": 0.0, "wz": 0.0}

        self.velocity_history = {"time": [], "vx": [], "vy": [], "wz": []}
        self.history_max_len = 300
        self.plot_start_time = time.time()

        self._build_ui()
        self._setup_timers()

    def _build_ui(self) -> None:
        root = FocusClearWidget()
        root.setFocusPolicy(Qt.ClickFocus)
        main = QVBoxLayout(root)

        status_box = QGroupBox("Connection")
        status_layout = QHBoxLayout(status_box)
        self.status_label = QLabel(f"Connecting: {self.uri}")
        self.ack_label = QLabel("Last ACK: none")
        status_layout.addWidget(self.status_label, 2)
        status_layout.addWidget(self.ack_label, 1)

        cfg_box = QGroupBox("Speed Config")
        cfg_form = QFormLayout(cfg_box)

        self.linear_x_spin = QDoubleSpinBox()
        self.linear_x_spin.setRange(0.1, 3.0)
        self.linear_x_spin.setDecimals(2)
        self.linear_x_spin.setSingleStep(0.1)
        self.linear_x_spin.setValue(self.args.linear_max_x)

        self.linear_y_spin = QDoubleSpinBox()
        self.linear_y_spin.setRange(0.1, 3.0)
        self.linear_y_spin.setDecimals(2)
        self.linear_y_spin.setSingleStep(0.1)
        self.linear_y_spin.setValue(self.args.linear_max_y)

        self.angular_spin = QDoubleSpinBox()
        self.angular_spin.setRange(0.1, 5.0)
        self.angular_spin.setDecimals(2)
        self.angular_spin.setSingleStep(0.1)
        self.angular_spin.setValue(self.args.angular_max)

        cfg_form.addRow("Linear max X", self.linear_x_spin)
        cfg_form.addRow("Linear max Y", self.linear_y_spin)
        cfg_form.addRow("Angular max", self.angular_spin)

        button_box = QGroupBox("Action Buttons")
        grid = QGridLayout(button_box)
        actions = [
            ("Damp", "damp"),
            ("Stand Up", "stand_up"),
            ("Stand Down", "stand_down"),
            ("Balance Stand", "balance_stand"),
            ("Recovery Stand", "recovery_stand"),
            ("Stop Move", "stop_move"),
        ]
        for idx, (label, cmd) in enumerate(actions):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, c=cmd: self._send_action(c))
            row = idx // 3
            col = idx % 3
            grid.addWidget(btn, row, col)

        self.velocity_label = QLabel("velocity: vx=0.000 vy=0.000 wz=0.000")

        self.fig = Figure(figsize=(9, 3), dpi=80)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Speed")
        self.ax.set_title("Velocity Over Time")
        self.ax.grid(True, alpha=0.3)
        self.line_vx, = self.ax.plot([], [], label="vx", color="red")
        self.line_vy, = self.ax.plot([], [], label="vy", color="green")
        self.line_wz, = self.ax.plot([], [], label="wz", color="blue")
        self.ax.legend(loc="upper left")
        self.fig.tight_layout()

        main.addWidget(status_box)
        main.addWidget(cfg_box)
        main.addWidget(button_box)
        main.addWidget(self.velocity_label)
        main.addWidget(self.canvas)

        self.setCentralWidget(root)

    def _setup_timers(self) -> None:
        self.connect_poll_timer = QTimer(self)
        self.connect_poll_timer.timeout.connect(self._poll_connect)
        self.connect_poll_timer.start(100)

        self.control_timer = QTimer(self)
        self.control_timer.timeout.connect(self._control_tick)
        interval_ms = max(1, int(self.args.send_interval * 1000))
        self.control_timer.start(interval_ms)

        self.ack_poll_timer = QTimer(self)
        self.ack_poll_timer.timeout.connect(self._poll_pending_futures)
        self.ack_poll_timer.start(40)

        self.plot_update_timer = QTimer(self)
        self.plot_update_timer.timeout.connect(self._update_plot)
        self.plot_update_timer.start(100)

        self.pending_futures: list[tuple[Future, bool]] = []

    def _poll_connect(self) -> None:
        if not self.connect_future.done():
            return

        self.connect_poll_timer.stop()
        try:
            _ = self.connect_future.result(timeout=0)
            self.status_label.setText(f"Connected: {self.uri}")
        except Exception as error:
            self.status_label.setText(f"Connect failed: {error}")

    def _poll_pending_futures(self) -> None:
        next_futures: list[tuple[Future, bool]] = []
        for fut, show_ack in self.pending_futures:
            if not fut.done():
                next_futures.append((fut, show_ack))
                continue

            try:
                ack = fut.result(timeout=0)
                if show_ack:
                    self.ack_label.setText(
                        f"Last ACK: ok={ack.get('ok')} code={ack.get('code')} msg={ack.get('message', '')}"
                    )
            except Exception as error:
                self.ack_label.setText(f"Last ACK: failed {error}")

        self.pending_futures = next_futures

    def _update_plot(self) -> None:
        if not self.velocity_history["time"]:
            return
        self.line_vx.set_data(self.velocity_history["time"], self.velocity_history["vx"])
        self.line_vy.set_data(self.velocity_history["time"], self.velocity_history["vy"])
        self.line_wz.set_data(self.velocity_history["time"], self.velocity_history["wz"])
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

    def _send_action(self, cmd: str) -> None:
        fut = self.bridge.send_command(cmd, {}, wait_ack=True)
        self.pending_futures.append((fut, True))

    def _calculate_velocity(self) -> Dict[str, float]:
        current = time.time()
        vmax_x = self.linear_x_spin.value()
        vmax_y = self.linear_y_spin.value()
        wmax = self.angular_spin.value()
        accel = self.args.accel_factor
        vel = {"vx": 0.0, "vy": 0.0, "wz": 0.0}

        if self.key_pressed["w"]:
            vel["vx"] = min((current - self.key_press_time["w"]) * accel, vmax_x)
        elif self.key_pressed["s"]:
            vel["vx"] = -min((current - self.key_press_time["s"]) * accel, vmax_x)

        if self.key_pressed["a"]:
            vel["vy"] = min((current - self.key_press_time["a"]) * accel, vmax_y)
        elif self.key_pressed["d"]:
            vel["vy"] = -min((current - self.key_press_time["d"]) * accel, vmax_y)

        if self.key_pressed["q"]:
            vel["wz"] = min((current - self.key_press_time["q"]) * accel, wmax)
        elif self.key_pressed["e"]:
            vel["wz"] = -min((current - self.key_press_time["e"]) * accel, wmax)

        return vel

    def _control_tick(self) -> None:
        if not self.connect_future.done() or self.connect_future.cancelled():
            return

        prev = self.last_velocity.copy()
        vel = self._calculate_velocity()
        self.last_velocity = vel
        self.velocity_label.setText(
            f"velocity: vx={vel['vx']:.3f} vy={vel['vy']:.3f} wz={vel['wz']:.3f}"
        )

        elapsed = time.time() - self.plot_start_time
        self.velocity_history["time"].append(elapsed)
        self.velocity_history["vx"].append(vel["vx"])
        self.velocity_history["vy"].append(vel["vy"])
        self.velocity_history["wz"].append(vel["wz"])

        if len(self.velocity_history["time"]) > self.history_max_len:
            self.velocity_history["time"].pop(0)
            self.velocity_history["vx"].pop(0)
            self.velocity_history["vy"].pop(0)
            self.velocity_history["wz"].pop(0)

        moving = any(v != 0.0 for v in vel.values())
        if moving:
            fut = self.bridge.send_command(
                "move",
                {"vx": vel["vx"], "vy": vel["vy"], "wz": vel["wz"]},
                wait_ack=False,
            )
            self.pending_futures.append((fut, False))
        elif any(v != 0.0 for v in prev.values()):
            fut = self.bridge.send_command("stop_move", {}, wait_ack=False)
            self.pending_futures.append((fut, False))

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            return

        mapped = self._map_key(event.key())
        if mapped is not None and not self.key_pressed[mapped]:
            self.key_pressed[mapped] = True
            self.key_press_time[mapped] = time.time()

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            return

        mapped = self._map_key(event.key())
        if mapped is not None:
            self.key_pressed[mapped] = False
            self.key_press_time[mapped] = 0.0

    @staticmethod
    def _map_key(key: int) -> Optional[str]:
        keymap = {
            Qt.Key_W: "w",
            Qt.Key_S: "s",
            Qt.Key_A: "a",
            Qt.Key_D: "d",
            Qt.Key_Q: "q",
            Qt.Key_E: "e",
        }
        return keymap.get(key)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # Best-effort final stop before closing.
        try:
            fut = self.bridge.send_command("stop_move", {}, wait_ack=False)
            self.pending_futures.append((fut, False))
        except Exception:
            pass

        self.bridge.stop()
        event.accept()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PySide6 GUI speed controller for Go2")
    parser.add_argument("--host", default="127.0.0.1", help="Robot/server host")
    parser.add_argument("--port", type=int, default=9001, help="WebSocket server port")
    parser.add_argument("--path", default="/control", help="WebSocket path for control")
    parser.add_argument("--ack-timeout", type=float, default=2.0, help="Seconds waiting for ACK")
    parser.add_argument("--ttl-ms", type=int, default=500, help="TTL for command message")
    parser.add_argument("--heartbeat-interval", type=float, default=1.0, help="Heartbeat interval seconds")

    parser.add_argument("--send-interval", type=float, default=0.01, help="Speed command interval seconds")
    parser.add_argument("--linear-max-x", type=float, default=1.0, help="Max linear speed for vx")
    parser.add_argument("--linear-max-y", type=float, default=0.7, help="Max linear speed for vy")
    parser.add_argument("--linear-max", type=float, default=None, help="Deprecated: set both linear x/y max")
    parser.add_argument("--angular-max", type=float, default=1.5, help="Max angular speed for wz")
    parser.add_argument("--accel-factor", type=float, default=2.0, help="Acceleration factor")
    args = parser.parse_args()
    if args.linear_max is not None:
        args.linear_max_x = args.linear_max
        args.linear_max_y = args.linear_max
    return args


def main() -> None:
    args = parse_args()
    app = QApplication(sys.argv)
    window = SpeedControlWindow(args)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
