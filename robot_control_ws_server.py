import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import websockets
from websockets.server import WebSocketServerProtocol

SDK_REPO_PATH = Path.home() / "unitree_sdk2_python"
if str(SDK_REPO_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_REPO_PATH))

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.obstacles_avoid.obstacles_avoid_client import ObstaclesAvoidClient
from unitree_sdk2py.go2.sport.sport_client import SportClient

PROTOCOL_VERSION = "go2.ctrl.v1"


def now_ms() -> int:
    return int(time.time() * 1000)


def make_ack(message_id: str, ok: bool, code: str, message: str, queue_depth: int = 0) -> Dict:
    return {
        "version": PROTOCOL_VERSION,
        "type": "ack",
        "id": message_id,
        "timestamp_ms": now_ms(),
        "ok": ok,
        "code": code,
        "message": message,
        "queue_depth": queue_depth,
    }


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class ControlPipelineServer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.command_queue: asyncio.Queue[Tuple[Dict, WebSocketServerProtocol]] = asyncio.Queue(
            maxsize=max(1, args.control_queue_size)
        )
        self.sport_client: Optional[SportClient] = None
        self.obstacles_client: Optional[ObstaclesAvoidClient] = None
        self.last_heartbeat_ms: Dict[WebSocketServerProtocol, int] = {}

    async def start(self) -> None:
        ChannelFactoryInitialize(0, self.args.sdk_interface)

        self.sport_client = SportClient()
        self.sport_client.SetTimeout(self.args.sport_timeout)
        self.sport_client.Init()

        self.obstacles_client = ObstaclesAvoidClient()
        self.obstacles_client.SetTimeout(self.args.sport_timeout)
        self.obstacles_client.Init()

        print(f"[CTL] SDK interface={self.args.sdk_interface}")
        print(f"[CTL] ws://{self.args.host}:{self.args.port}{self.args.path}")

        async with websockets.serve(self.on_client, self.args.host, self.args.port, max_size=2_000_000):
            await asyncio.gather(self.execute_loop(), self.heartbeat_guard_loop())

    async def on_client(self, websocket: WebSocketServerProtocol, path: str) -> None:
        if path != self.args.path:
            await websocket.send(json.dumps(make_ack("path", False, "BAD_PATH", f"expected {self.args.path}")))
            await websocket.close()
            return

        self.last_heartbeat_ms[websocket] = now_ms()
        print(f"[CTL] client connected: {websocket.remote_address}")

        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps(make_ack("unknown", False, "BAD_JSON", "invalid JSON")))
                    continue

                ack = await self.handle_message(message, websocket)
                await websocket.send(json.dumps(ack, ensure_ascii=False))
        finally:
            self.last_heartbeat_ms.pop(websocket, None)
            print(f"[CTL] client disconnected: {websocket.remote_address}")

    async def handle_message(self, message: Dict, websocket: WebSocketServerProtocol) -> Dict:
        message_type = str(message.get("type", ""))
        message_id = str(message.get("id", "unknown"))

        if message_type == "hello":
            return make_ack(message_id, True, "HELLO", "connected")

        if message_type == "heartbeat":
            self.last_heartbeat_ms[websocket] = now_ms()
            return make_ack(message_id, True, "ALIVE", "heartbeat")

        if message_type != "command":
            return make_ack(message_id, False, "BAD_TYPE", "type must be command/heartbeat/hello")

        command_name = message.get("cmd")
        params = message.get("params", {})
        if not isinstance(command_name, str) or not command_name:
            return make_ack(message_id, False, "BAD_COMMAND", "cmd must be non-empty string")
        if not isinstance(params, dict):
            return make_ack(message_id, False, "BAD_PARAMS", "params must be object")

        envelope = {
            "id": message_id,
            "cmd": command_name,
            "params": params,
            "timestamp_ms": int(message.get("timestamp_ms", now_ms())),
            "ttl_ms": int(message.get("ttl_ms", self.args.default_ttl_ms)),
        }

        try:
            self.command_queue.put_nowait((envelope, websocket))
        except asyncio.QueueFull:
            return make_ack(message_id, False, "QUEUE_FULL", "control queue is full")

        return make_ack(message_id, True, "QUEUED", "command queued", self.command_queue.qsize())

    async def execute_loop(self) -> None:
        assert self.sport_client is not None
        assert self.obstacles_client is not None

        while True:
            envelope, websocket = await self.command_queue.get()
            command_id = str(envelope.get("id", "unknown"))
            command_name = str(envelope.get("cmd", ""))
            params = envelope.get("params", {})

            timestamp_ms = int(envelope.get("timestamp_ms", now_ms()))
            ttl_ms = max(0, int(envelope.get("ttl_ms", self.args.default_ttl_ms)))
            if ttl_ms > 0 and now_ms() - timestamp_ms > ttl_ms:
                print(f"[CTL] drop stale id={command_id} cmd={command_name}")
                self.command_queue.task_done()
                continue

            try:
                if command_name == "damp":
                    self.sport_client.Damp()
                elif command_name == "stand_up":
                    self.sport_client.StandUp()
                elif command_name == "stand_down":
                    self.sport_client.StandDown()
                elif command_name == "stop_move":
                    self.sport_client.StopMove()
                elif command_name == "balance_stand":
                    self.sport_client.BalanceStand()
                elif command_name == "recovery_stand":
                    self.sport_client.RecoveryStand()
                elif command_name == "move":
                    raw_vx = float(params.get("vx", 0.0))
                    raw_vy = float(params.get("vy", 0.0))
                    raw_wz = float(params.get("wz", 0.0))

                    vx = clamp(raw_vx, self.args.max_vx)
                    vy = clamp(raw_vy, self.args.max_vy)
                    wz = clamp(raw_wz, self.args.max_wz)

                    self.sport_client.Move(vx, vy, wz)

                    duration_ms = int(params.get("duration_ms", 0))
                    if duration_ms > 0:
                        await asyncio.sleep(duration_ms / 1000.0)
                        self.sport_client.StopMove()
                elif command_name == "set_obstacle_avoidance":
                    enabled = bool(params.get("enabled", True))
                    self.obstacles_client.SwitchSet(enabled)
                elif command_name == "sport_api":
                    api_id = int(params.get("api_id", -1))
                    parameter = params.get("parameter", {})
                    if not isinstance(parameter, dict):
                        parameter = {}
                    self._execute_sport_api(api_id, parameter)
                else:
                    print(f"[CTL] unsupported id={command_id} cmd={command_name}")

                await self.send_event(websocket, "info", "executed", command_id, f"{command_name} executed")
                print(f"[CTL] executed id={command_id} cmd={command_name}")
            except Exception as error:
                await self.send_event(websocket, "error", "failed", command_id, str(error))
                print(f"[CTL] failed id={command_id} cmd={command_name} err={error}")
            finally:
                self.command_queue.task_done()

    def _execute_sport_api(self, api_id: int, parameter: Dict) -> None:
        assert self.sport_client is not None

        if api_id == 1001:
            self.sport_client.Damp()
        elif api_id == 1002:
            self.sport_client.BalanceStand()
        elif api_id == 1003:
            self.sport_client.StopMove()
        elif api_id == 1004:
            self.sport_client.StandUp()
        elif api_id == 1005:
            self.sport_client.StandDown()
        elif api_id == 1006:
            self.sport_client.RecoveryStand()
        elif api_id == 1009:
            self.sport_client.Sit()
        elif api_id == 1010:
            self.sport_client.RiseSit()
        elif api_id == 1015:
            self.sport_client.SpeedLevel(int(parameter.get("data", 0)))
        elif api_id == 1016:
            self.sport_client.Hello()
        elif api_id == 1017:
            self.sport_client.Stretch()
        elif api_id == 1020:
            self.sport_client.Content()
        elif api_id == 1022:
            self.sport_client.Dance1()
        elif api_id == 1023:
            self.sport_client.Dance2()
        elif api_id == 1027:
            self.sport_client.SwitchJoystick(bool(parameter.get("data", True)))
        elif api_id == 1028:
            self.sport_client.Pose(bool(parameter.get("data", True)))
        elif api_id == 1029:
            self.sport_client.Scrape()
        elif api_id == 1030:
            self.sport_client.FrontFlip()
        elif api_id == 1031:
            self.sport_client.FrontJump()
        elif api_id == 1032:
            self.sport_client.FrontPounce()
        elif api_id == 1036:
            self.sport_client.Heart()
        elif api_id in (1042, 2041):
            self.sport_client.LeftFlip()
        elif api_id in (1044, 2043):
            self.sport_client.BackFlip()
        elif api_id == 1302:
            self.sport_client.CrossStep(True)
        elif api_id == 1304:
            self.sport_client.FreeBound(True)
        elif api_id == 1305:
            raise ValueError("MoonWalk (api_id=1305) is not available in sdkpy SportClient")
        else:
            raise ValueError(f"Unsupported sport api_id: {api_id}")

    async def send_event(
        self,
        websocket: WebSocketServerProtocol,
        level: str,
        event: str,
        related_id: str,
        detail: str,
    ) -> None:
        payload = {
            "version": PROTOCOL_VERSION,
            "type": "event",
            "id": f"e-{now_ms()}",
            "timestamp_ms": now_ms(),
            "level": level,
            "event": event,
            "related_id": related_id,
            "detail": detail,
        }
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    async def heartbeat_guard_loop(self) -> None:
        assert self.sport_client is not None
        while True:
            now = now_ms()
            timeout_ms = int(self.args.heartbeat_timeout_s * 1000)
            for websocket, hb in list(self.last_heartbeat_ms.items()):
                if now - hb > timeout_ms:
                    print("[CTL] heartbeat timeout -> stop_move")
                    try:
                        self.sport_client.StopMove()
                    except Exception:
                        pass
                    self.last_heartbeat_ms[websocket] = now
            await asyncio.sleep(0.2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Go2 control pipeline server (WS JSON)")
    parser.add_argument("--sdk-interface", default="eth0", help="Unitree SDK network interface")
    parser.add_argument("--host", default="0.0.0.0", help="WebSocket bind host")
    parser.add_argument("--port", type=int, default=9001, help="WebSocket bind port")
    parser.add_argument("--path", default="/control", help="WebSocket path")
    parser.add_argument("--control-queue-size", type=int, default=100, help="Max pending commands")
    parser.add_argument("--sport-timeout", type=float, default=3.0, help="SportClient timeout seconds")
    parser.add_argument("--default-ttl-ms", type=int, default=500, help="Default command TTL")
    parser.add_argument("--heartbeat-timeout-s", type=float, default=2.0, help="Heartbeat timeout seconds")
    parser.add_argument("--max-vx", type=float, default=1.0, help="Max |vx|")
    parser.add_argument("--max-vy", type=float, default=1.0, help="Max |vy|")
    parser.add_argument("--max-wz", type=float, default=1.5, help="Max |wz|")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    server = ControlPipelineServer(args)
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())
