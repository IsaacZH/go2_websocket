import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from foxglove_websocket import run_cancellable
from foxglove_websocket.server import FoxgloveServer

SDK_REPO_PATH = Path.home() / "unitree_sdk2_python"
if str(SDK_REPO_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_REPO_PATH))

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.video.video_client import VideoClient


COMPRESSED_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "timestamp": {
            "type": "object",
            "properties": {
                "sec": {"type": "integer"},
                "nsec": {"type": "integer"},
            },
            "required": ["sec", "nsec"],
        },
        "frame_id": {"type": "string"},
        "format": {"type": "string"},
        "data": {"type": "string", "contentEncoding": "base64"},
    },
    "required": ["timestamp", "frame_id", "format", "data"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream Go2 camera to Foxglove WebSocket without ROS")
    parser.add_argument("--sdk-interface", default="eth0", help="Unitree SDK network interface, e.g. eth0/wlan0")
    parser.add_argument("--ws-interface", default="wlan0", help="Interface used to expose websocket service")
    parser.add_argument("--bind-host", default=None, help="WebSocket bind IP; default uses --ws-interface IPv4")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--topic", default="/go2/front_camera/compressed", help="Foxglove topic name")
    parser.add_argument("--frame-id", default="go2_front_camera", help="Frame id in message")
    parser.add_argument("--fps", type=float, default=15.0, help="Publish FPS")
    parser.add_argument("--jpeg-quality", type=int, default=80, help="JPEG quality 1-100")
    parser.add_argument("--name", default="go2-camera-direct", help="Foxglove server name")
    return parser.parse_args()


def get_interface_ipv4(interface: str) -> Optional[str]:
    try:
        command = ["ip", "-4", "-o", "addr", "show", "dev", interface]
        output = subprocess.check_output(command, text=True).strip()
        if not output:
            return None
        first = output.splitlines()[0]
        cidr = first.split()[3]
        return cidr.split("/")[0]
    except Exception:
        return None


def decode_sdk_frame(data: bytes) -> Optional[np.ndarray]:
    image_data = np.frombuffer(bytes(data), dtype=np.uint8)
    return cv2.imdecode(image_data, cv2.IMREAD_COLOR)


def encode_jpeg(image: np.ndarray, quality: int) -> Optional[bytes]:
    quality = max(1, min(100, quality))
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    return encoded.tobytes()


async def stream_camera(args: argparse.Namespace) -> None:
    bind_host = args.bind_host or get_interface_ipv4(args.ws_interface) or "0.0.0.0"

    ChannelFactoryInitialize(0, args.sdk_interface)
    client = VideoClient()
    client.SetTimeout(3.0)
    client.Init()

    print(f"[SDK] interface={args.sdk_interface}")
    print(f"[WS ] listening on ws://{bind_host}:{args.port}")
    print(f"[WS ] topic={args.topic}")

    async with FoxgloveServer(bind_host, args.port, args.name, supported_encodings=["json"]) as server:
        channel_id = await server.add_channel(
            {
                "topic": args.topic,
                "encoding": "json",
                "schemaName": "foxglove.CompressedImage",
                "schemaEncoding": "jsonschema",
                "schema": json.dumps(COMPRESSED_IMAGE_SCHEMA),
            }
        )

        period = 1.0 / args.fps if args.fps > 0 else 1.0 / 15.0

        while True:
            code, data = client.GetImageSample()
            if code != 0:
                await asyncio.sleep(0.05)
                continue

            image = decode_sdk_frame(data)
            if image is None:
                await asyncio.sleep(0.01)
                continue

            jpeg_bytes = encode_jpeg(image, args.jpeg_quality)
            if jpeg_bytes is None:
                await asyncio.sleep(0.01)
                continue

            now_ns = time.time_ns()
            sec = now_ns // 1_000_000_000
            nsec = now_ns % 1_000_000_000

            payload = {
                "timestamp": {"sec": sec, "nsec": nsec},
                "frame_id": args.frame_id,
                "format": "jpeg",
                "data": base64.b64encode(jpeg_bytes).decode("ascii"),
            }

            await server.send_message(channel_id, now_ns, json.dumps(payload).encode("utf-8"))
            await asyncio.sleep(period)


def main() -> None:
    args = parse_args()
    run_cancellable(stream_camera(args))


if __name__ == "__main__":
    main()
