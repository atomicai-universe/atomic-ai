"""Live Bedrock Nova Sonic smoke test (manual/opt-in — NOT part of pytest).

Opens a REAL bidirectional Nova Sonic session through the same Strands BidiAgent
the app uses, streams a synthetic 16 kHz mono PCM16 clip (chunked like a mic),
and prints the output event types it receives. This verifies live model access,
IAM permissions, region, and the audio-input framing end-to-end.

Run inside the backend container:
    /opt/venv/bin/python scripts/nova_sonic_live_smoke.py

Skips (exit 0) with a clear message when AWS creds are absent. Never used in the
automated suite — it needs network + Bedrock model access.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import struct
import sys


def _synth_pcm16(seconds: float = 1.0, freq: int = 220, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )


async def main() -> int:
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        print("SKIP: no AWS credentials in the environment.")
        return 0

    try:
        from strands.experimental.bidi import BidiAgent, BidiAudioInputEvent
        from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: Strands bidi SDK unavailable: {type(exc).__name__}: {exc}")
        return 0

    from app.config import get_settings

    settings = get_settings()
    model_id = settings.NOVA_SONIC_MODEL_ID
    region = settings.AWS_REGION
    print(f"Connecting to Nova Sonic model={model_id} region={region} ...")

    model = BidiNovaSonicModel(model_id=model_id, client_config={"region": region})
    agent = BidiAgent(model=model, tools=[], system_prompt="You are a test agent. Reply briefly.")

    try:
        await agent.start()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not open Nova Sonic stream: {type(exc).__name__}: {exc}")
        return 1
    print("Stream opened. Sending synthetic audio ...")

    pcm = _synth_pcm16(seconds=1.0)
    chunk = 512 * 2  # ~512 frames of 16-bit mono
    event_types: dict[str, int] = {}

    async def sender():
        for i in range(0, len(pcm), chunk):
            await agent.send(
                BidiAudioInputEvent(
                    audio=base64.b64encode(pcm[i : i + chunk]).decode("ascii"),
                    format="pcm",
                    sample_rate=16000,
                    channels=1,
                )
            )
            await asyncio.sleep(0.03)

    async def receiver():
        try:
            async for event in agent.receive():
                et = event.get("type") if isinstance(event, dict) else type(event).__name__
                event_types[et] = event_types.get(et, 0) + 1
        except Exception as exc:  # noqa: BLE001
            print(f"receiver ended: {type(exc).__name__}: {exc}")

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())
    try:
        await send_task
        await asyncio.sleep(3.0)  # let the model respond
    finally:
        recv_task.cancel()
        try:
            await agent.stop()
        except Exception:  # noqa: BLE001
            pass

    print("Event types received:", event_types or "<none>")
    if event_types:
        print("PASS: live Nova Sonic session produced output events.")
        return 0
    print("WARN: stream opened but no output events observed within the window.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
