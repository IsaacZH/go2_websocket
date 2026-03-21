# Go2 Control Pipeline (Standalone, Non-Foxglove)

This pipeline is independent from `dds_to_foxglove.py`.

- Telemetry/visualization stays in `dds_to_foxglove.py` unchanged.
- Control runs in a separate WebSocket server: `robot_control_ws_server.py`.

## Protocol

See `control_protocol_v1.md`.

## Robot side start

```bash
cd /home/jetson/isaac_ws
/usr/bin/python3 -m pip install -r requirements_foxglove_ws.txt
/usr/bin/python3 robot_control_ws_server.py --sdk-interface eth0 --host 0.0.0.0 --port 9001 --path /control
```

## PC side demo client

```bash
cd /home/jetson/isaac_ws
/usr/bin/python3 demo_ws_control_client.py --host <robot-ip> --port 9001 --path /control
```

## Notes

- Supported commands: `damp`, `stand_up`, `stand_down`, `move`, `stop_move`, `balance_stand`, `recovery_stand`.
- `move` params: `vx`, `vy`, `wz`, optional `duration_ms`.
- If heartbeat is missing for `heartbeat-timeout-s`, server triggers `StopMove()`.
