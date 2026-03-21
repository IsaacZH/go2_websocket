# Go2 Remote Control Protocol (WS JSON) v1

This protocol is designed for **GUI/PC remote control** over WebSocket and can coexist with telemetry visualization on the same network architecture.

## 1) Transport

- WebSocket text frames (`UTF-8` JSON)
- Suggested endpoint: `ws://<robot-ip>:8765/control`
- Alternative (same port, Foxglove service mode): map the same JSON payload into Foxglove service request body

## 2) Envelope

All messages share a common envelope:

```json
{
  "version": "go2.ctrl.v1",
  "type": "command|ack|event|heartbeat|hello",
  "id": "uuid-or-client-seq",
  "timestamp_ms": 1710000000000
}
```

## 3) Client -> Server: `hello`

```json
{
  "version": "go2.ctrl.v1",
  "type": "hello",
  "id": "h-001",
  "timestamp_ms": 1710000000000,
  "client": {
    "name": "operator-laptop",
    "app": "go2-demo-controller",
    "protocol": "go2.ctrl.v1"
  }
}
```

## 4) Client -> Server: `command`

```json
{
  "version": "go2.ctrl.v1",
  "type": "command",
  "id": "c-1001",
  "timestamp_ms": 1710000000100,
  "require_ack": true,
  "ttl_ms": 500,
  "cmd": "move",
  "params": {
    "vx": 0.3,
    "vy": 0.0,
    "wz": 0.0,
    "duration_ms": 800
  }
}
```

### Supported `cmd`

- `damp`
- `stand_up`
- `stand_down`
- `move` (`vx`, `vy`, `wz`, optional `duration_ms`)
- `stop_move`
- `balance_stand`
- `recovery_stand`

## 5) Server -> Client: `ack`

```json
{
  "version": "go2.ctrl.v1",
  "type": "ack",
  "id": "c-1001",
  "timestamp_ms": 1710000000123,
  "ok": true,
  "code": "QUEUED",
  "message": "command queued",
  "queue_depth": 2
}
```

Failure example:

```json
{
  "version": "go2.ctrl.v1",
  "type": "ack",
  "id": "c-1001",
  "timestamp_ms": 1710000000123,
  "ok": false,
  "code": "INVALID_PARAM",
  "message": "vx out of range [-1.0,1.0]"
}
```

## 6) Server -> Client: `event` (optional)

```json
{
  "version": "go2.ctrl.v1",
  "type": "event",
  "id": "e-9001",
  "timestamp_ms": 1710000000500,
  "level": "info|warn|error",
  "event": "executed",
  "related_id": "c-1001",
  "detail": "move executed"
}
```

## 7) Heartbeat

Client sends every 1 second:

```json
{
  "version": "go2.ctrl.v1",
  "type": "heartbeat",
  "id": "hb-001",
  "timestamp_ms": 1710000001000
}
```

Server replies:

```json
{
  "version": "go2.ctrl.v1",
  "type": "ack",
  "id": "hb-001",
  "timestamp_ms": 1710000001002,
  "ok": true,
  "code": "ALIVE",
  "message": "heartbeat"
}
```

## 8) Safety recommendations

- Require `stand_up` before `move`.
- Apply server-side velocity limits, e.g. `|vx|, |vy| <= 1.0`, `|wz| <= 1.5`.
- Drop stale commands when `now - timestamp_ms > ttl_ms`.
- Keep an emergency `stop_move` command with highest priority.
- If heartbeat timeout (e.g. >2s), auto-stop robot.

## 9) Mapping to Unitree `SportClient`

- `damp` -> `SportClient.Damp()`
- `stand_up` -> `SportClient.StandUp()`
- `stand_down` -> `SportClient.StandDown()`
- `move` -> `SportClient.Move(vx, vy, wz)`
- `stop_move` -> `SportClient.StopMove()`
- `balance_stand` -> `SportClient.BalanceStand()`
- `recovery_stand` -> `SportClient.RecoveryStand()`
