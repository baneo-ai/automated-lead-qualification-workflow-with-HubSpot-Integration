# webhook_server.py
import os, json, time
from typing import Any, Dict

from dotenv import load_dotenv
load_dotenv()  

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from hubspot_vapi_agent import (
    handle_hubspot_webhook,
    process_vapi_end_of_call,
)

app = FastAPI(title="HubSpot ↔ Vapi Orchestrator")

VAPI_WEBHOOK_SECRET = os.getenv("VAPI_WEBHOOK_SECRET")  

# ────────────── naive hourly idempotency ──────────────
SEEN = set()
def idempotent(key: str) -> bool:
    bucket = f"{key}:{int(time.time())//3600}"
    if bucket in SEEN:
        return False
    if len(SEEN) > 10_000:
        SEEN.clear()
    SEEN.add(bucket)
    return True

@app.get("/health")
async def health():
    return {"status": "healthy"}

# ───────────────── HubSpot webhook ────────────────────
@app.post("/webhook/hubspot")
async def hubspot(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    print("RAW HUBSPOT BODY:", raw.decode(errors="ignore")[:1200])

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "bad json"}, status_code=200)

    # App webhooks commonly send an array of events
    if isinstance(payload, list):
        print(f"[HS] {len(payload)} event(s)")
        for ev in payload:
            idem = ev.get("eventId") or ev.get("objectId") or json.dumps(ev, sort_keys=True)
            if idempotent(f"hs:{idem}"):
                bg.add_task(handle_hubspot_webhook, ev)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Workflow webhook can be a single object
    if isinstance(payload, dict):
        idem = payload.get("eventId") or payload.get("objectId") or json.dumps(payload, sort_keys=True)
        if idempotent(f"hs:{idem}"):
            bg.add_task(handle_hubspot_webhook, payload)
        return JSONResponse({"status": "accepted"}, status_code=202)

    return JSONResponse({"status": "ignored"}, status_code=200)

# ─────────────────── Vapi webhook ─────────────────────
@app.post("/webhook/vapi")
async def vapi(request: Request, bg: BackgroundTasks):
    # Optional secret check
    if VAPI_WEBHOOK_SECRET:
        incoming = request.headers.get("x-vapi-secret")
        if incoming != VAPI_WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="unauthorized")

    raw = await request.body()
    print("RAW VAPI BODY:", raw.decode(errors="ignore")[:1500])

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"status": "bad json"}, status_code=200)

    # Vapi events arrive under message:{...}
    msg = (payload or {}).get("message", {}) or {}
    event_type   = msg.get("type")
    call         = msg.get("call", {}) or {}
    call_id      = call.get("id")
    ended_reason = msg.get("endedReason")
    artifact     = msg.get("artifact", {}) or {}
    analysis     = msg.get("analysis", {}) or {}

    # Note: transcripts might be in artifact; summary often in analysis
    transcript   = artifact.get("transcript") or ""
    summary      = analysis.get("summary") or artifact.get("summary") or ""
    answers      = analysis.get("structuredData") or {}
    metadata     = call.get("metadata", {}) or {}

    print(f"[VAPI] type={event_type} call_id={call_id} ended_reason={ended_reason}")
    print("SUMMARY:", summary[:300])
    print("TRANSCRIPT LEN:", len(transcript))

    idem = f"{event_type}:{call_id}:{msg.get('timestamp','')}"
    if not idempotent(f"vapi:{idem}"):
        return JSONResponse({"status": "duplicate"}, status_code=200)

    # Only act on end-of-call
    if event_type == "end-of-call-report":
        normalized = {
            "type": event_type,
            "call_id": call_id,
            "endedReason": ended_reason,
            "summary": summary,
            "transcript": transcript,
            "answers": answers,
            "metadata": metadata,
        }
        bg.add_task(process_vapi_end_of_call, normalized)

    return JSONResponse({"status": "accepted"}, status_code=202)