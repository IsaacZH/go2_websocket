import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock
from typing import Optional, Tuple

import cv2
import numpy as np
from foxglove_websocket import run_cancellable
from foxglove_websocket.server import FoxgloveServer

SDK_REPO_PATH = Path.home() / "unitree_sdk2_python"
if str(SDK_REPO_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_REPO_PATH))

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
from unitree_sdk2py.go2.video.video_client import VideoClient
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_


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

IMU_SCHEMA = {
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
        "tick": {"type": "integer"},
        "quaternion": {"type": "array", "items": {"type": "number"}},
        "gyroscope": {"type": "array", "items": {"type": "number"}},
        "accelerometer": {"type": "array", "items": {"type": "number"}},
        "rpy": {"type": "array", "items": {"type": "number"}},
        "temperature": {"type": "integer"},
    },
    "required": [
        "timestamp",
        "tick",
        "quaternion",
        "gyroscope",
        "accelerometer",
        "rpy",
        "temperature",
    ],
}

MOTOR_STATES_SCHEMA = {
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
        "tick": {"type": "integer"},
        "motors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "mode": {"type": "integer"},
                    "q": {"type": "number"},
                    "dq": {"type": "number"},
                    "ddq": {"type": "number"},
                    "tau_est": {"type": "number"},
                    "temperature": {"type": "integer"},
                    "lost": {"type": "integer"},
                },
                "required": ["index", "mode", "q", "dq", "ddq", "tau_est", "temperature", "lost"],
            },
        },
    },
    "required": ["timestamp", "tick", "motors"],
}

FOXGLOVE_POINT_CLOUD_SCHEMA = {
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
        "point_stride": {"type": "integer"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "offset": {"type": "integer"},
                    "type": {"type": "integer"},
                    "count": {"type": "integer"},
                },
                "required": ["name", "offset", "type", "count"],
            },
        },
        "data": {"type": "string", "contentEncoding": "base64"},
        "point_count": {"type": "integer"},
    },
    "required": ["timestamp", "frame_id", "point_stride", "fields", "data", "point_count"],
}


class LowStateCache:
    def __init__(self):
        self._lock = Lock()
        self._latest: Optional[LowState_] = None

    def update(self, msg: LowState_) -> None:
        with self._lock:
            self._latest = msg

    def get(self) -> Optional[LowState_]:
        with self._lock:
            return self._latest


class PointCloudCache:
    def __init__(self):
        self._lock = Lock()
        self._latest: Optional[PointCloud2_] = None

    def update(self, msg: PointCloud2_) -> None:
        with self._lock:
            self._latest = msg

    def get(self) -> Optional[PointCloud2_]:
        with self._lock:
            return self._latest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream Go2 camera to Foxglove WebSocket without ROS")
    parser.add_argument("--sdk-interface", default="eth0", help="Unitree SDK network interface, e.g. eth0/wlan0")
    parser.add_argument("--ws-interface", default="wlan0", help="Interface used to expose websocket service")
    parser.add_argument("--bind-host", default=None, help="WebSocket bind IP; default uses --ws-interface IPv4")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--topic", default="/go2/front_camera/compressed", help="Foxglove topic name")
    parser.add_argument("--imu-topic", default="/go2/imu", help="Foxglove IMU topic name")
    parser.add_argument("--motor-topic", default="/go2/motor_states", help="Foxglove motor state topic name")
    parser.add_argument("--lidar-topic", default="/go2/lidar/points", help="Foxglove lidar point cloud topic name")
    parser.add_argument("--lidar-dds-topic", default="rt/utlidar/cloud", help="DDS topic of PointCloud2 from Unitree lidar")
    parser.add_argument("--frame-id", default="go2_front_camera", help="Frame id in message")
    parser.add_argument("--fps", type=float, default=15.0, help="Publish FPS")
    parser.add_argument("--state-fps", type=float, default=50.0, help="Publish rate for IMU/motor topics")
    parser.add_argument("--lidar-fps", type=float, default=10.0, help="Publish rate for lidar point cloud topic")
    parser.add_argument("--motor-count", type=int, default=12, help="How many motors to publish (Go2 usually 12)")
    parser.add_argument("--lidar-max-points", type=int, default=12000, help="Max point count per lidar frame (downsample if larger)")
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


def to_time_fields(now_ns: int) -> dict:
    return {"sec": now_ns // 1_000_000_000, "nsec": now_ns % 1_000_000_000}


def pointcloud_to_payload(msg: PointCloud2_, max_points: int) -> Tuple[dict, int]:
    if max_points < 1:
        max_points = 1

    point_step = int(msg.point_step)
    width = int(msg.width)
    height = int(msg.height)
    total_points = width * height
    data_bytes = bytes(msg.data)

    sampled_data = data_bytes
    sampled_points = total_points

    contiguous = (int(msg.row_step) == width * point_step) and point_step > 0 and total_points > 0
    if contiguous and total_points > max_points:
        step = max(1, total_points // max_points)
        reduced = bytearray()
        for index in range(0, total_points, step):
            begin = index * point_step
            end = begin + point_step
            reduced.extend(data_bytes[begin:end])
        sampled_data = bytes(reduced)
        sampled_points = len(sampled_data) // point_step

    stamp = msg.header.stamp
    payload = {
        "timestamp": {"sec": int(stamp.sec), "nsec": int(stamp.nanosec)},
        "frame_id": msg.header.frame_id,
        "point_stride": point_step,
        "fields": [
            {
                "name": field.name,
                "offset": int(field.offset),
                "type": int(field.datatype),
                "count": int(field.count),
            }
            for field in msg.fields
        ],
        "data": base64.b64encode(sampled_data).decode("ascii"),
        "point_count": sampled_points,
    }
    return payload, sampled_points


async def stream_camera_loop(
    args: argparse.Namespace,
    server: FoxgloveServer,
    channel_id: int,
    client: VideoClient,
) -> None:
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
        payload = {
            "timestamp": to_time_fields(now_ns),
            "frame_id": args.frame_id,
            "format": "jpeg",
            "data": base64.b64encode(jpeg_bytes).decode("ascii"),
        }

        await server.send_message(channel_id, now_ns, json.dumps(payload).encode("utf-8"))
        await asyncio.sleep(period)


async def stream_state_loop(
    args: argparse.Namespace,
    server: FoxgloveServer,
    imu_channel_id: int,
    motor_channel_id: int,
    cache: LowStateCache,
) -> None:
    period = 1.0 / args.state_fps if args.state_fps > 0 else 1.0 / 50.0

    while True:
        state = cache.get()
        if state is None:
            await asyncio.sleep(0.02)
            continue

        now_ns = time.time_ns()

        imu_payload = {
            "timestamp": to_time_fields(now_ns),
            "tick": int(state.tick),
            "quaternion": [float(value) for value in state.imu_state.quaternion],
            "gyroscope": [float(value) for value in state.imu_state.gyroscope],
            "accelerometer": [float(value) for value in state.imu_state.accelerometer],
            "rpy": [float(value) for value in state.imu_state.rpy],
            "temperature": int(state.imu_state.temperature),
        }
        await server.send_message(imu_channel_id, now_ns, json.dumps(imu_payload).encode("utf-8"))

        max_count = min(max(args.motor_count, 1), len(state.motor_state))
        motors = []
        for index in range(max_count):
            motor = state.motor_state[index]
            motors.append(
                {
                    "index": index,
                    "mode": int(motor.mode),
                    "q": float(motor.q),
                    "dq": float(motor.dq),
                    "ddq": float(motor.ddq),
                    "tau_est": float(motor.tau_est),
                    "temperature": int(motor.temperature),
                    "lost": int(motor.lost),
                }
            )

        motor_payload = {
            "timestamp": to_time_fields(now_ns),
            "tick": int(state.tick),
            "motors": motors,
        }
        await server.send_message(motor_channel_id, now_ns, json.dumps(motor_payload).encode("utf-8"))

        await asyncio.sleep(period)


async def stream_lidar_loop(
    args: argparse.Namespace,
    server: FoxgloveServer,
    lidar_channel_id: int,
    cache: PointCloudCache,
) -> None:
    period = 1.0 / args.lidar_fps if args.lidar_fps > 0 else 1.0 / 10.0

    while True:
        cloud = cache.get()
        if cloud is None:
            await asyncio.sleep(0.05)
            continue

        now_ns = time.time_ns()
        payload, _sampled_points = pointcloud_to_payload(cloud, args.lidar_max_points)
        await server.send_message(lidar_channel_id, now_ns, json.dumps(payload).encode("utf-8"))
        await asyncio.sleep(period)


async def stream_camera(args: argparse.Namespace) -> None:
    bind_host = args.bind_host or get_interface_ipv4(args.ws_interface) or "0.0.0.0"

    ChannelFactoryInitialize(0, args.sdk_interface)

    low_state_cache = LowStateCache()
    pointcloud_cache = PointCloudCache()

    def on_low_state(msg: LowState_) -> None:
        low_state_cache.update(msg)

    def on_lidar_pointcloud(msg: PointCloud2_) -> None:
        pointcloud_cache.update(msg)

    low_state_subscriber = ChannelSubscriber("rt/lowstate", LowState_)
    low_state_subscriber.Init(on_low_state, 10)

    lidar_subscriber = ChannelSubscriber(args.lidar_dds_topic, PointCloud2_)
    lidar_subscriber.Init(on_lidar_pointcloud, 3)

    client = VideoClient()
    client.SetTimeout(3.0)
    client.Init()

    print(f"[SDK] interface={args.sdk_interface}")
    print(f"[WS ] listening on ws://{bind_host}:{args.port}")
    print(f"[WS ] image topic={args.topic}")
    print(f"[WS ] imu topic={args.imu_topic}")
    print(f"[WS ] motor topic={args.motor_topic}")
    print(f"[DDS] lidar topic={args.lidar_dds_topic}")
    print(f"[WS ] lidar topic={args.lidar_topic}")

    async with FoxgloveServer(bind_host, args.port, args.name, supported_encodings=["json"]) as server:
        image_channel_id = await server.add_channel(
            {
                "topic": args.topic,
                "encoding": "json",
                "schemaName": "foxglove.CompressedImage",
                "schemaEncoding": "jsonschema",
                "schema": json.dumps(COMPRESSED_IMAGE_SCHEMA),
            }
        )

        imu_channel_id = await server.add_channel(
            {
                "topic": args.imu_topic,
                "encoding": "json",
                "schemaName": "go2.IMUState",
                "schemaEncoding": "jsonschema",
                "schema": json.dumps(IMU_SCHEMA),
            }
        )

        motor_channel_id = await server.add_channel(
            {
                "topic": args.motor_topic,
                "encoding": "json",
                "schemaName": "go2.MotorStates",
                "schemaEncoding": "jsonschema",
                "schema": json.dumps(MOTOR_STATES_SCHEMA),
            }
        )

        lidar_channel_id = await server.add_channel(
            {
                "topic": args.lidar_topic,
                "encoding": "json",
                "schemaName": "foxglove.PointCloud",
                "schemaEncoding": "jsonschema",
                "schema": json.dumps(FOXGLOVE_POINT_CLOUD_SCHEMA),
            }
        )

        await asyncio.gather(
            stream_camera_loop(args, server, image_channel_id, client),
            stream_state_loop(args, server, imu_channel_id, motor_channel_id, low_state_cache),
            stream_lidar_loop(args, server, lidar_channel_id, pointcloud_cache),
        )


def main() -> None:
    args = parse_args()
    run_cancellable(stream_camera(args))


if __name__ == "__main__":
    main()
