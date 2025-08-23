# Automated Lead Qualification Workflow with HubSpot Integration

This repository documents **two approaches** for building an automated lead‑qualification system that integrates **HubSpot** and **VAPI**:

1) **n8n Version (No‑Code)** – Workflow‑driven automation using n8n + VAPI + HubSpot (+ Twilio for numbers).  
2) **LangGraph Version (Python + FastAPI)** – Code‑based, multi‑agent orchestration with LangGraph, FastAPI, and optional LLM analysis.

Both versions implement the same business outcome; choose **n8n** for visual/no‑code customization or **LangGraph** for code‑first control and extensibility.

---

## Use Case

The system automates how new leads are handled once they enter HubSpot.

1. **Lead created in HubSpot**  
   • Whenever a new contact is added in HubSpot (**status = NEW**), a workflow is triggered.

2. **Automated outbound call via VAPI**  
   • The system places a call to the lead using VAPI.  
   • A virtual agent runs the conversation and asks qualification questions (budget, authority, need, timeline, etc.).

3. **Call outcome analysis**  
   • At the end of the call, VAPI sends a call report (summary + transcript + structured data).  
   • The system classifies the lead as **Qualified**, **Unqualified**, or **Other**.

4. **Human vs. Voicemail distinction**  
   • If a human answers, the transcript and analysis reflect captured answers.  
   • If voicemail/no‑answer, the workflow updates HubSpot accordingly (e.g., **Attempted to Contact**, **No Connect**).

5. **Update HubSpot automatically**  
   • Updates the lead status (e.g., **Qualified → Open Deal**, **Unqualified**, **Attempted Contact/Connected**).  
   • Logs a **call engagement** inside the contact record and writes a professional **call summary** to a contact property.

---

## Pre‑requisites

1. **HubSpot**  
   - A Private App with CRM scopes (contacts read/write; companies read as needed).  
   - **Client ID**, **Client Secret**, and **Refresh Token** (OAuth).  
   - A text contact property (e.g., `contact_summary`) to store summaries.

2. **VAPI**  
   - Access to [vapi.ai](https://vapi.ai), an active workflow, and your **VAPI_API_KEY** + **VAPI_WORKFLOW_ID**.

3. **Tunnel (ngrok or similar)**  
   - Public URL to receive HubSpot + VAPI webhooks during local dev.

4. **Python 3.11+** (LangGraph version)  
   - `pip` available to install dependencies.

5. **OpenAI API Key (optional)**  
   - Enables LLM‑powered analysis of transcripts. Without it, a heuristic fallback is used.

---

## Repository Contents

### n8n Version
- **Workflow Files (JSON)**  
  - Lead qualification with HubSpot integration  
  - Call event reporting & summarization  
  - Voice agent with prompt workflow (VAPI)
- **Integration Documentation (PDF)** – Architecture, configuration, flow, and setup.
- **Notes**  
  - Implemented entirely in n8n.  
  - Voice agent configured via VAPI.  
  - Phone numbers typically from Twilio.  
  - All sensitive keys/IDs are **redacted**.

### LangGraph Version (Code)
- `langgraph-version/`
  - `hubspot_vapi_agent.py` – LangGraph nodes, HubSpot helpers, VAPI helpers, LLM analysis (optional).
  - `webhook_server.py` – FastAPI server exposing `/webhook/hubspot` and `/webhook/vapi`.
  - `.env.example` – Example environment variables template.
  - `requirements.txt` – Python dependencies.
- **Note on Logging**  
  - Debug logs are **intentionally verbose** for local troubleshooting and may include PII (transcripts, CRM data).  
  - **Remove/Sanitize** before deploying to any shared/production environment.

---

## Setup Instructions (LangGraph Version)

1. **Clone the repo**
   git clone https://github.com/baneo-ai/automated-lead-qualification-workflow-with-HubSpot-Integration.git
   cd automated-lead-qualification-workflow-with-HubSpot-Integration/langgraph-version

3. Create & activate a virtual environment
   python3 -m venv venv
   source venv/bin/activate

4. Install dependencies
   pip install -r requirements.txt

5. Configure environment
   Copy .env.example → .env and fill in:
	•	HUBSPOT_CLIENT_ID, HUBSPOT_CLIENT_SECRET, HUBSPOT_REFRESH_TOKEN
	•	HUBSPOT_ACCESS_TOKEN (initial; auto‑refresh is handled)
	•	CALL_SUMMARY_PROPERTY (e.g., contact_summary)
	•	HS_STATUS_OPEN_DEAL, HS_STATUS_UNQUALIFIED, HS_STATUS_CONTACTED (match your portal)
	•	VAPI_API_KEY, VAPI_WORKFLOW_ID, VAPI_WEBHOOK_SECRET (optional)
	•	BASE_URL (your public ngrok URL, no trailing slash)

6. Start the FastAPI server
   uvicorn webhook_server:app --reload --port 8000

7. Expose locally via ngrok
   ngrok http 8000
   Set BASE_URL=https://<your-ngrok-host>.ngrok-free.app in .env

8. Register webhooks
   HubSpot → point contact.creation webhook to: https://<your-ngrok-url>/webhook/hubspot
   VAPI → set webhook to: https://<your-ngrok-url>/webhook/vapi (If you set VAPI_WEBHOOK_SECRET, also configure the same secret header in VAPI)

## Example Workflows (Curl)
   Notes
	•	Example IDs (eventId, portalId, etc.) below are placeholders.
	•	Replace <CONTACT_ID> with a valid HubSpot contact ID for your portal.
	•	Replace <ngrok-url> with your running tunnel URL.
	•	If you enabled a VAPI secret, add -H "x-vapi-secret: <your-secret>" to the VAPI curl.

 HubSpot → New Contact (simulate webhook event)
 curl -X POST "https://<ngrok-url>/webhook/hubspot" \
  -H "Content-Type: application/json" \
  -d '[
    {
      "eventId": 11111,
      "subscriptionId": 22222,
      "portalId": 3333333,
      "appId": 44444,
      "occurredAt": 1234567890000,
      "subscriptionType": "contact.creation",
      "attemptNumber": 0,
      "objectId": "<CONTACT_ID>",
      "changeFlag": "NEW",
      "changeSource": "INTEGRATION",
      "sourceId": "55555"
    }
  ]'

  VAPI → End of Call (simulate end-of-call-report)
  curl -X POST "https://<ngrok-url>/webhook/vapi" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "type": "end-of-call-report",
      "timestamp": 999,
      "endedReason": "completed",
      "call": { "id": "sim-001", "metadata": { "lead_id": "<CONTACT_ID>", "name": "Test Lead" } },
      "artifact": { "transcript": "Prospect confirmed budget, authority, wants this week." },
      "analysis": {
        "summary": "Qualified: strong budget, decision maker, timeline within 7 days.",
        "structuredData": { "budget": "high", "need": "clear", "authority": true, "timing_days": 7 }
      }
    }
  }'

  ## Security and Privacy
	•	Do not commit real keys or tokens. Use .env locally and a secret manager in production.
	•	Debug logs may include PII (e.g., transcripts, phone, email). Remove or sanitize logs before deploying to shared environments.
	•	If using VAPI_WEBHOOK_SECRET, verify it in the server and configure the same value in VAPI.

 ## Choosing an approach
	•	n8n – fastest to customize visually; ideal for teams that prefer no‑code and workflow editors.
	•	LangGraph – more control and extensibility for developers; supports multi‑agent orchestration and LLM‑based analysis.
   
