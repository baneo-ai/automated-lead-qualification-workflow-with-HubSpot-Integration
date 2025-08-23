# hubspot_vapi_agent.py
import os, json, time, requests
from typing import Dict, List, Any, TypedDict, Optional

from dotenv import load_dotenv
load_dotenv()  

# --- LangGraph for HS → Vapi orchestration
from langgraph.graph import StateGraph, END

# --- LLM call 
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

# ───────────────────────────── ENV ─────────────────────────────
HUBSPOT_ACCESS_TOKEN   = os.getenv("HUBSPOT_ACCESS_TOKEN")     
HUBSPOT_CLIENT_ID      = os.getenv("HUBSPOT_CLIENT_ID")
HUBSPOT_CLIENT_SECRET  = os.getenv("HUBSPOT_CLIENT_SECRET")
HUBSPOT_REFRESH_TOKEN  = os.getenv("HUBSPOT_REFRESH_TOKEN")

VAPI_API_KEY           = os.getenv("VAPI_API_KEY")
VAPI_WORKFLOW_ID       = os.getenv("VAPI_WORKFLOW_ID")
BASE_URL               = os.getenv("BASE_URL", "").rstrip("/")

CALL_SUMMARY_PROPERTY  = os.getenv("CALL_SUMMARY_PROPERTY", "contact_summary")
HS_STATUS_OPEN_DEAL    = os.getenv("HS_STATUS_OPEN_DEAL", "OPEN_DEAL")
HS_STATUS_UNQUALIFIED  = os.getenv("HS_STATUS_UNQUALIFIED", "UNQUALIFIED")
HS_STATUS_CONTACTED    = os.getenv("HS_STATUS_CONTACTED", "CONNECTED")  

OPENAI_API_KEY         = os.getenv("OPENAI_API_KEY")

# ───────────────── HubSpot OAuth auto‑refresh ─────────────────
class HubSpotTokenManager:
    """Holds/refreshes HubSpot OAuth access token."""
    def __init__(self, access_token: Optional[str]):
        self._access_token = access_token

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    def refresh(self) -> str:
        if not (HUBSPOT_CLIENT_ID and HUBSPOT_CLIENT_SECRET and HUBSPOT_REFRESH_TOKEN):
            raise RuntimeError(
                "HubSpot OAuth credentials missing: HUBSPOT_CLIENT_ID / HUBSPOT_CLIENT_SECRET / HUBSPOT_REFRESH_TOKEN"
            )
        token_url = "https://api.hubapi.com/oauth/v1/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": HUBSPOT_CLIENT_ID,
            "client_secret": HUBSPOT_CLIENT_SECRET,
            "refresh_token": HUBSPOT_REFRESH_TOKEN,
        }
        r = requests.post(token_url, data=data, timeout=30)
        r.raise_for_status()
        new_token = r.json().get("access_token")
        if not new_token:
            raise RuntimeError("HubSpot refresh did not return access_token")
        self._access_token = new_token
        return new_token

TOKEN = HubSpotTokenManager(HUBSPOT_ACCESS_TOKEN)

def _is_expired_auth(resp: requests.Response) -> bool:
    if resp.status_code != 401:
        return False
    try:
        j = resp.json()
        return j.get("category") in ("EXPIRED_AUTHENTICATION", "INVALID_AUTHENTICATION")
    except Exception:
        return False

def hubspot_request(method: str, path: str, **kwargs) -> requests.Response:
    """Send HubSpot API request with auto‑refresh on expired token. `path` begins with /crm/... or other root path."""
    base = "https://api.hubapi.com"
    headers = kwargs.pop("headers", {})
    if TOKEN.access_token:
        headers["Authorization"] = f"Bearer {TOKEN.access_token}"
    headers.setdefault("Content-Type", "application/json")

    resp = requests.request(method, base + path, headers=headers, timeout=30, **kwargs)
    if _is_expired_auth(resp):
        TOKEN.refresh()
        headers["Authorization"] = f"Bearer {TOKEN.access_token}"
        resp = requests.request(method, base + path, headers=headers, timeout=30, **kwargs)
    return resp

# ─────────────────────── Helpers: HubSpot ─────────────────────
def get_contact_details(contact_id: str) -> Dict[str, Any]:
    """Fetch contact details from HubSpot (v3)."""
    try:
        r = hubspot_request("GET", f"/crm/v3/objects/contacts/{contact_id}")
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": f"Failed to fetch contact: {e}"}

def update_contact_status(contact_id: str, status: str, call_summary: Optional[str]) -> Dict[str, Any]:
    """Patch hs_lead_status + optional summary property on contact (v3)."""
    props = {"hs_lead_status": status}
    if call_summary:
        props[CALL_SUMMARY_PROPERTY] = call_summary
    try:
        r = hubspot_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props})
        r.raise_for_status()
        return {"success": True, "message": f"Contact {contact_id} updated to {status}"}
    except requests.RequestException as e:
        return {"error": f"Failed to update contact: {e}"}

def create_hs_logged_call(contact_id: str, body_text: str,
                          status: str = "COMPLETED",
                          direction: str = "OUTBOUND",
                          timestamp_ms: Optional[int] = None) -> Dict[str, Any]:
    """
    Create a 'Logged call' card on the contact timeline (Engagements v1) to match n8n’s output.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    payload = {
        "engagement": {
            "active": True,
            "type": "CALL",
            "timestamp": timestamp_ms
        },
        "associations": {
            "contactIds": [str(contact_id)]
        },
        "metadata": {
            "body": body_text or "",
            "status": status,  
            "fromNumber": "",
            "toNumber": "",
            "durationMilliseconds": 0,
        }
    }

    # v1 uses different root; call requests directly but reuse TOKEN + refresh logic
    headers = {"Content-Type": "application/json"}
    if TOKEN.access_token:
        headers["Authorization"] = f"Bearer {TOKEN.access_token}"
    url = "https://api.hubapi.com/engagements/v1/engagements"

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        if _is_expired_auth(r):
            TOKEN.refresh()
            headers["Authorization"] = f"Bearer {TOKEN.access_token}"
            r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        return {"success": True, "id": r.json().get("engagement", {}).get("id")}
    except Exception as e:
        return {"error": f"Failed to create logged call: {e}"}

# ───────────────────── Helpers: Vapi client ───────────────────
def initiate_vapi_call(phone_number: str, contact_name: str, lead_id: str) -> Dict[str, Any]:
    """Start a Vapi call via Workflow; metadata.lead_id is the HubSpot contactId."""
    try:
        url = "https://api.vapi.ai/v1/calls"
        headers = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "workflow_id": VAPI_WORKFLOW_ID,
            "to": phone_number,
            "metadata": {"lead_id": lead_id, "name": contact_name},
            "webhook_url": f"{BASE_URL}/webhook/vapi"
        }
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": f"Failed to initiate Vapi call: {e}"}

# ─────────────── Optional LLM (with safe fallback) ────────────
_llm: Optional[ChatOpenAI] = None
if OPENAI_API_KEY:
    try:
        _llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    except Exception as e:
        print("[LLM] init failed; using heuristic:", e)
        _llm = None

def analyze_call_result(transcript: str, summary: str, ended_reason: str) -> Dict[str, Any]:
    """Classify call + produce a compact CRM summary."""
    if not _llm:
        text = f"{summary} {transcript} {ended_reason}".lower()
        qualified = "qualified" if any(k in text for k in ["forward", "approved", "qualified"]) else "unqualified"
        return {
            "connected": True if text else False,
            "qualified": qualified,
            "reasoning": "Heuristic (no LLM).",
            "hubspot_summary": summary or (transcript[:950] if transcript else "No summary provided.")
        }

    prompt = f"""Return ONLY valid JSON.
EndedReason: {ended_reason}
Summary: {summary}
Transcript: {transcript}

Fields:
- connected: boolean
- qualified: "qualified" | "unqualified" | "not_applicable"
- reasoning: short string
- hubspot_summary: compact professional summary
"""
    try:
        resp = _llm.invoke([HumanMessage(content=prompt)])
        content = resp.content.strip()
        if not content.startswith("{"):
            a, b = content.find("{"), content.rfind("}")
            content = content[a:b+1] if a != -1 and b != -1 else "{}"
        return json.loads(content or "{}")
    except Exception as e:
        return {
            "connected": False,
            "qualified": "not_applicable",
            "reasoning": f"Analysis failed: {e}",
            "hubspot_summary": summary or (transcript[:950] if transcript else "Call analysis failed.")
        }

# ───────────────────── LangGraph workflow ─────────────────────
class AgentState(TypedDict):
    contact_data: Dict[str, Any]
    call_result: Dict[str, Any]
    analysis_result: Dict[str, Any]
    hubspot_update: Dict[str, Any]
    messages: List[Any]
    error: str

def contact_processor(state: AgentState) -> AgentState:
    contact = state.get("contact_data", {})
    props = contact.get("properties", {}) or {}
    status = (props.get("hs_lead_status") or "").upper()
    phone  = props.get("phone") or ""
    first  = props.get("firstname") or ""
    last   = props.get("lastname") or ""
    cid    = str(contact.get("id") or "")

    if status != "NEW":
        return {**state, "error": f"Contact status is {status}, not NEW. Skipping."}
    if not phone:
        return {**state, "error": "No phone on contact."}

    return {
        **state,
        "contact_data": {
            "id": cid,
            "phone": phone,
            "full_name": (f"{first} {last}").strip() or "there",
        }
    }

def call_initiator(state: AgentState) -> AgentState:
    if state.get("error"): return state
    c = state["contact_data"]
    res = initiate_vapi_call(c["phone"], c["full_name"], c["id"])
    if "error" in res: return {**state, "error": res["error"]}
    return {**state, "call_result": res}

def error_handler(state: AgentState) -> AgentState:
    if state.get("error"): print("[Workflow Error]", state["error"])
    return state

def create_workflow():
    g = StateGraph(AgentState)
    g.add_node("contact_processor", contact_processor)
    g.add_node("call_initiator", call_initiator)
    g.add_node("error_handler", error_handler)

    g.set_entry_point("contact_processor")
    g.add_conditional_edges("contact_processor", lambda s: "call_initiator" if "error" not in s else "error_handler")
    g.add_conditional_edges("call_initiator",    lambda s: END             if "error" not in s else "error_handler")
    g.add_edge("error_handler", END)
    return g.compile()

# ─────────────── Entry points used by the server ──────────────
def handle_hubspot_webhook(event: Dict[str, Any]) -> None:
    """HubSpot app/webhook (contact.creation). Starts a Vapi call with LangGraph."""
    if event.get("subscriptionType") != "contact.creation":
        print(f"[HubSpot] Ignoring subscriptionType={event.get('subscriptionType')}")
        return

    contact_id = str(event.get("objectId") or "")
    if not contact_id:
        print("[HubSpot] Missing objectId")
        return

    contact = get_contact_details(contact_id)
    if "error" in contact:
        print("[HubSpot] fetch failed:", contact["error"])
        return

    wf = create_workflow()
    initial: AgentState = {
        "contact_data": contact,
        "call_result": {},
        "analysis_result": {},
        "hubspot_update": {},
        "messages": [],
        "error": ""
    }
    result = wf.invoke(initial)
    print("Lead processed:", {
        "id": contact.get("id"),
        "phone": contact.get("properties", {}).get("phone"),
        "qualified": result.get("analysis_result", {}).get("qualified"),
        "hs_update": result.get("hubspot_update")
    })

def process_vapi_end_of_call(normalized: Dict[str, Any]) -> None:
    """Called by /webhook/vapi after normalizing the payload."""
    lead_id     = normalized.get("metadata", {}).get("lead_id")
    summary     = normalized.get("summary") or ""
    transcript  = normalized.get("transcript") or ""
    ended       = normalized.get("endedReason") or ""

    if not lead_id:
        print("[Vapi] Missing lead_id; cannot update HubSpot.")
        return

    analysis = analyze_call_result(transcript, summary, ended)
    q = (analysis.get("qualified") or "not_applicable").lower()
    if q == "qualified":
        hs_status = HS_STATUS_OPEN_DEAL
    elif q == "unqualified":
        hs_status = HS_STATUS_UNQUALIFIED
    else:
        hs_status = HS_STATUS_CONTACTED

    # Update contact property + status
    upd = update_contact_status(str(lead_id), hs_status, analysis.get("hubspot_summary"))
    print("[HubSpot] update result:", upd)

    # Create a “Logged call” card to match n8n
    card = create_hs_logged_call(
        contact_id=str(lead_id),
        body_text=analysis.get("hubspot_summary") or summary or "Call summary unavailable.",
        status="COMPLETED",
        direction="OUTBOUND"
    )
    print("[HubSpot] logged call result:", card)