import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import websockets

PROTOCOL_VERSION = "go2.ctrl.v1"


@dataclass
class TestOption:
    name: str
    id: int


OPTION_LIST = [
    TestOption(name="damp", id=0),
    TestOption(name="stand_up", id=1),
    TestOption(name="stand_down", id=2),
    TestOption(name="move_forward", id=3),
    TestOption(name="move_lateral", id=4),
    TestOption(name="move_rotate", id=5),
    TestOption(name="stop_move", id=6),
    TestOption(name="balance_stand", id=9),
    TestOption(name="recovery_stand", id=10),
]


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
                "name": "demo-operator",
                "app": "go2-demo-controller",
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


class UserInterface:
    def __init__(self):
        self.test_option_: Optional[TestOption] = None

    @staticmethod
    def _to_int(value: str) -> Optional[int]:
        try:
            return int(value)
        except ValueError:
            return None

    def choose(self) -> None:
        user_input = input("Enter id or name (list/quit): \n").strip()

        if user_input == "list":
            self.test_option_ = None
            for option in OPTION_LIST:
                print(f"{option.name}, id: {option.id}")
            return

        if user_input in {"quit", "exit", "q"}:
            self.test_option_ = TestOption(name="quit", id=-1)
            return

        value = self._to_int(user_input)
        for option in OPTION_LIST:
            if user_input == option.name or value == option.id:
                self.test_option_ = option
                print(f"Selected: {option.name}, id: {option.id}")
                return

        print("No matching test option found.")
        self.test_option_ = None


async def heartbeat_task(client: RemoteControllerClient, interval: float) -> None:
    while True:
        try:
            await client.send_heartbeat()
        except Exception as error:
            print(f"[heartbeat] failed: {error}")
        await asyncio.sleep(interval)


def build_move_params(option_id: int) -> Dict[str, Any]:
    if option_id == 3:
        return {"vx": 0.3, "vy": 0.0, "wz": 0.0, "duration_ms": 800}
    if option_id == 4:
        return {"vx": 0.0, "vy": 0.3, "wz": 0.0, "duration_ms": 800}
    if option_id == 5:
        return {"vx": 0.0, "vy": 0.0, "wz": 0.5, "duration_ms": 800}
    return {}


def map_option_to_command(option: TestOption) -> Dict[str, Any]:
    if option.id == 0:
        return {"cmd": "damp", "params": {}}
    if option.id == 1:
        return {"cmd": "stand_up", "params": {}}
    if option.id == 2:
        return {"cmd": "stand_down", "params": {}}
    if option.id in {3, 4, 5}:
        return {"cmd": "move", "params": build_move_params(option.id)}
    if option.id == 6:
        return {"cmd": "stop_move", "params": {}}
    if option.id == 9:
        return {"cmd": "balance_stand", "params": {}}
    if option.id == 10:
        return {"cmd": "recovery_stand", "params": {}}
    raise ValueError(f"Unsupported option id: {option.id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Demo WebSocket remote control client for Go2")
    parser.add_argument("--host", default="127.0.0.1", help="Robot/server host")
    parser.add_argument("--port", type=int, default=9001, help="WebSocket server port")
    parser.add_argument("--path", default="/control", help="WebSocket path for control")
    parser.add_argument("--ack-timeout", type=float, default=2.0, help="Seconds waiting for ACK")
    parser.add_argument("--ttl-ms", type=int, default=500, help="TTL for command message")
    parser.add_argument("--heartbeat-interval", type=float, default=1.0, help="Heartbeat interval in seconds")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    uri = f"ws://{args.host}:{args.port}{args.path}"
    client = RemoteControllerClient(uri, ack_timeout=args.ack_timeout, ttl_ms=args.ttl_ms)

    print("WARNING: Ensure there are no obstacles around robot before remote control.")
    print(f"Connecting to {uri}")
    await client.connect()
    print("Connected. Type 'list' to print options.")

    hb = asyncio.create_task(heartbeat_task(client, args.heartbeat_interval))

    ui = UserInterface()

    try:
        while True:
            ui.choose()
            if ui.test_option_ is None:
                continue
            if ui.test_option_.id == -1:
                print("Exit requested.")
                break

            try:
                req = map_option_to_command(ui.test_option_)
            except ValueError as error:
                print(error)
                continue

            ack = await client.send_command(req["cmd"], req["params"])
            print(f"ACK: ok={ack.get('ok')} code={ack.get('code')} msg={ack.get('message')}")
    finally:
        hb.cancel()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
