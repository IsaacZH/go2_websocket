import argparse
import asyncio
import json
import math
import sys
import time
import uuid
from typing import Any, Dict, Optional

import websockets

try:
    from pynput import keyboard as pynput_keyboard
except ImportError:
    print("Error: 'pynput' library not installed!")
    print("Please install it with: pip install pynput")
    sys.exit(1)

PROTOCOL_VERSION = "go2.ctrl.v1"


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

        for future in self._pending_acks.values():
            if not future.done():
                future.cancel()
        self._pending_acks.clear()

    async def send_hello(self, wait_ack: bool = False) -> Dict[str, Any]:
        hello_id = self._msg_id("h")
        hello = {
            "version": PROTOCOL_VERSION,
            "type": "hello",
            "id": hello_id,
            "timestamp_ms": self._now_ms(),
            "client": {
                "name": "keyboard-operator",
                "app": "go2-keyboard-controller",
                "protocol": PROTOCOL_VERSION,
            },
        }
        await self._send_json(hello)
        if wait_ack:
            return await self._wait_ack(hello_id)
        return {"ok": True, "code": "SENT", "id": hello_id}

    async def send_heartbeat(self) -> None:
        heartbeat = {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "id": self._msg_id("hb"),
            "timestamp_ms": self._now_ms(),
        }
        await self._send_json(heartbeat)

    async def send_command(
        self,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
        wait_ack: bool = False,
    ) -> Dict[str, Any]:
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
        if wait_ack:
            return await self._wait_ack(command_id)
        return {"ok": True, "code": "SENT", "id": command_id}

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
                    future = self._pending_acks.get(msg_id)
                    if future is not None and not future.done():
                        future.set_result(msg)
                elif msg.get("type") == "event":
                    print(
                        f"[event] level={msg.get('level')} event={msg.get('event')} detail={msg.get('detail')}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[reader] stopped: {error}")


class KeyboardVelocityController:
    def __init__(
        self,
        client: RemoteControllerClient,
        send_interval: float,
        speed_linear_max: float,
        speed_angular_max: float,
        acceleration_factor: float,
    ):
        self.client = client
        self.running = True
        self.send_interval = send_interval
        self.speed_linear_max = speed_linear_max
        self.speed_angular_max = speed_angular_max
        self.acceleration_factor = acceleration_factor

        self.key_press_time: Dict[str, float] = {
            "w": 0.0,
            "s": 0.0,
            "a": 0.0,
            "d": 0.0,
            "q": 0.0,
            "e": 0.0,
            "ctrl": 0.0,
            "space": 0.0,
        }
        self.key_pressed: Dict[str, bool] = {
            "w": False,
            "s": False,
            "a": False,
            "d": False,
            "q": False,
            "e": False,
            "ctrl": False,
            "space": False,
        }

        self.last_key_pressed = self.key_pressed.copy()
        self.last_velocity = {"vx": 0.0, "vy": 0.0, "wz": 0.0}
        self.last_send_time = 0.0
        self.in_move_mode = False

    def on_key_press(self, key_name: str) -> None:
        if key_name not in self.key_pressed:
            return
        if not self.key_pressed[key_name]:
            self.key_pressed[key_name] = True
            self.key_press_time[key_name] = time.time()

    def on_key_release(self, key_name: str) -> None:
        if key_name not in self.key_pressed:
            return
        self.key_pressed[key_name] = False
        self.key_press_time[key_name] = 0.0

    def stop_all(self) -> None:
        for key_name in self.key_pressed:
            self.key_pressed[key_name] = False
            self.key_press_time[key_name] = 0.0

    def _calculate_velocity(self) -> Dict[str, float]:
        current_time = time.time()
        velocity = {"vx": 0.0, "vy": 0.0, "wz": 0.0}

        if self.key_pressed["w"]:
            duration = current_time - self.key_press_time["w"]
            velocity["vx"] = min(duration * self.acceleration_factor, self.speed_linear_max)
        elif self.key_pressed["s"]:
            duration = current_time - self.key_press_time["s"]
            velocity["vx"] = -min(duration * self.acceleration_factor, self.speed_linear_max)

        if self.key_pressed["a"]:
            duration = current_time - self.key_press_time["a"]
            velocity["vy"] = min(duration * self.acceleration_factor, self.speed_linear_max)
        elif self.key_pressed["d"]:
            duration = current_time - self.key_press_time["d"]
            velocity["vy"] = -min(duration * self.acceleration_factor, self.speed_linear_max)

        if self.key_pressed["q"]:
            duration = current_time - self.key_press_time["q"]
            velocity["wz"] = min(duration * self.acceleration_factor, self.speed_angular_max)
        elif self.key_pressed["e"]:
            duration = current_time - self.key_press_time["e"]
            velocity["wz"] = -min(duration * self.acceleration_factor, self.speed_angular_max)

        self.last_key_pressed = self.key_pressed.copy()
        self.last_velocity = velocity.copy()
        return velocity

    async def send_movement_command(self) -> None:
        current_time = time.time()
        if current_time - self.last_send_time < self.send_interval:
            return
        self.last_send_time = current_time

        if self.key_pressed["space"] and not self.last_key_pressed["space"]:
            self.in_move_mode = not self.in_move_mode
            mode_cmd = "balance_stand" if self.in_move_mode else "damp"
            mode_name = "BalanceStand" if self.in_move_mode else "Damp"
            print(f"Space pressed: switching to {mode_name}")
            ack = await self.client.send_command(mode_cmd, {}, wait_ack=True)
            print(
                f"ACK: ok={ack.get('ok')} code={ack.get('code')} msg={ack.get('message', '')}"
            )
            self.last_key_pressed["space"] = True
            return

        if self.key_pressed["ctrl"]:
            if self.key_pressed["w"] and not self.last_key_pressed["w"]:
                await self._send_instant_action("stand_up", "Ctrl+W")
                self.last_key_pressed["w"] = True
                return
            if self.key_pressed["s"] and not self.last_key_pressed["s"]:
                await self._send_instant_action("stand_down", "Ctrl+S")
                self.last_key_pressed["s"] = True
                return
            if self.key_pressed["a"] and not self.last_key_pressed["a"]:
                await self._send_instant_action("recovery_stand", "Ctrl+A")
                self.last_key_pressed["a"] = True
                return
            if self.key_pressed["d"] and not self.last_key_pressed["d"]:
                await self._send_instant_action("balance_stand", "Ctrl+D")
                self.last_key_pressed["d"] = True
                return

        prev_velocity = self.last_velocity.copy()
        velocity = self._calculate_velocity()
        moving = any(value != 0.0 for value in velocity.values())

        if moving:
            print(
                "velocity: "
                f"vx={velocity['vx']:.3f}, vy={velocity['vy']:.3f}, wz={velocity['wz']:.3f}"
            )
            await self.client.send_command(
                "move",
                {
                    "vx": velocity["vx"],
                    "vy": velocity["vy"],
                    "wz": velocity["wz"],
                },
                wait_ack=False,
            )
        elif any(v != 0.0 for v in prev_velocity.values()):
            await self.client.send_command("stop_move", {}, wait_ack=False)

    async def _send_instant_action(self, cmd: str, trigger: str) -> None:
        print(f"{trigger}: sending {cmd}")
        ack = await self.client.send_command(cmd, {}, wait_ack=True)
        print(f"ACK: ok={ack.get('ok')} code={ack.get('code')} msg={ack.get('message', '')}")

    async def command_sender_task(self) -> None:
        print("Command sender started")
        try:
            while self.running:
                await self.send_movement_command()
                await asyncio.sleep(self.send_interval)
        finally:
            print("Command sender stopped")

    async def keyboard_control_with_pynput(self) -> None:
        self._print_help()

        pressed_chars = set()

        def on_press(key: Any) -> Optional[bool]:
            try:
                if key in (pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r):
                    self.on_key_press("ctrl")
                    return None

                if key == pynput_keyboard.Key.space:
                    self.on_key_press("space")
                    return None

                if key == pynput_keyboard.Key.esc:
                    self.running = False
                    self.stop_all()
                    return False

                if hasattr(key, "char") and key.char:
                    k = key.char.lower()
                    if k not in pressed_chars:
                        pressed_chars.add(k)
                        if k in {"w", "s", "a", "d", "q", "e"}:
                            self.on_key_press(k)
            except Exception:
                return None
            return None

        def on_release(key: Any) -> None:
            if key in (pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r):
                self.on_key_release("ctrl")
                return

            if key == pynput_keyboard.Key.space:
                self.on_key_release("space")
                return

            if hasattr(key, "char") and key.char:
                k = key.char.lower()
                if k in pressed_chars:
                    pressed_chars.discard(k)
                    if k in {"w", "s", "a", "d", "q", "e"}:
                        self.on_key_release(k)

        listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        sender_task = asyncio.create_task(self.command_sender_task())

        try:
            await sender_task
        finally:
            listener.stop()
            self.stop_all()
            await self.client.send_command("stop_move", {}, wait_ack=False)

    @staticmethod
    def _print_help() -> None:
        print("\n=== Keyboard Control Mode (Time-based Acceleration) ===")
        print("Motion:")
        print("  W - Forward")
        print("  S - Backward")
        print("  A - Strafe Left")
        print("  D - Strafe Right")
        print("  Q - Rotate Left")
        print("  E - Rotate Right")
        print("")
        print("Mode / action:")
        print("  Space - Toggle Damp / BalanceStand")
        print("  Ctrl+W - Stand Up")
        print("  Ctrl+S - Stand Down")
        print("  Ctrl+A - Recovery Stand")
        print("  Ctrl+D - Balance Stand")
        print("  ESC - Exit")
        print("\nFeature: Speed = Press Duration x Acceleration Factor")
        print("=" * 60)


async def heartbeat_task(client: RemoteControllerClient, interval: float) -> None:
    while True:
        try:
            await client.send_heartbeat()
        except Exception as error:
            print(f"[heartbeat] failed: {error}")
        await asyncio.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keyboard WebSocket controller for Go2")
    parser.add_argument("--host", default="127.0.0.1", help="Robot/server host")
    parser.add_argument("--port", type=int, default=9001, help="WebSocket server port")
    parser.add_argument("--path", default="/control", help="WebSocket path for control")
    parser.add_argument("--ack-timeout", type=float, default=2.0, help="Seconds waiting for ACK")
    parser.add_argument("--ttl-ms", type=int, default=500, help="TTL for command message")
    parser.add_argument("--heartbeat-interval", type=float, default=1.0, help="Heartbeat interval in seconds")

    parser.add_argument("--send-interval", type=float, default=0.01, help="Command send interval seconds")
    parser.add_argument("--linear-max", type=float, default=1.0, help="Max linear speed for vx/vy")
    parser.add_argument("--angular-max", type=float, default=1.5, help="Max angular speed for wz")
    parser.add_argument("--accel-factor", type=float, default=2.0, help="Acceleration factor")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    uri = f"ws://{args.host}:{args.port}{args.path}"

    print("WARNING: Ensure there are no obstacles around robot before remote control.")
    print(f"Connecting to {uri}")

    client = RemoteControllerClient(uri, ack_timeout=args.ack_timeout, ttl_ms=args.ttl_ms)
    await client.connect()
    print("Connected. Press ESC to quit.")

    hb_task = asyncio.create_task(heartbeat_task(client, args.heartbeat_interval))

    controller = KeyboardVelocityController(
        client=client,
        send_interval=args.send_interval,
        speed_linear_max=args.linear_max,
        speed_angular_max=args.angular_max,
        acceleration_factor=args.accel_factor,
    )

    try:
        await controller.keyboard_control_with_pynput()
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram interrupted by user")
