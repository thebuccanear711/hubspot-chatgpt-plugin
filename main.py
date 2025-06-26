import os
import requests
import openai
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from dateutil.parser import isoparse

# —— Load secrets —— 
load_dotenv()
HUBSPOT_TOKEN   = os.getenv("HUBSPOT_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
client          = openai.OpenAI(api_key=OPENAI_API_KEY)

# —— FastAPI app with custom OpenAPI —— 
app = FastAPI(
    title="HubSpot Briefing",
    version="1.0.0",
    openapi_url="/.well-known/openapi.json",
    docs_url=None,
    redoc_url=None
)

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        routes=app.routes,
    )
    # ← your real Render URL below:
    schema["servers"] = [
        {"url": "https://hubspot-chatgpt-plugin.onrender.com", "description": "Primary API server"}
    ]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

# —— Data models —— 

class ContactInfo(BaseModel):
    id: str
    firstname: str
    lastname: str
    email: Optional[str]
    jobtitle: Optional[str] = None

class DealInfo(BaseModel):
    id: str
    name: str
    amount: float
    stage: str
    closedate: datetime = None

class EngagementInfo(BaseModel):
    id: str
    type: str
    createdAt: datetime
    subject: str = None

    def __str__(self):
        return f"{self.createdAt.strftime('%b %d, %Y')} – {self.type} – {self.subject or 'No subject'}"

class CompanyBrief(BaseModel):
    id: str
    name: str
    domain: str
    website: Optional[str] = None
    industry: Optional[str] = None
    account_status: Optional[str] = None
    lifecycle_stage: Optional[str] = None

    contacts:           List[ContactInfo]
    deals_closed_won:   List[DealInfo]
    deals_closed_lost:  List[DealInfo]
    deals_expansion:    List[DealInfo]
    deals_resurrected:  List[DealInfo]
    deals_active:       List[DealInfo]
    recent_engagements: List[EngagementInfo]
    formatted_engagements: Optional[List[str]] = None

class BriefResponse(BaseModel):
    contact: ContactInfo
    company: CompanyBrief

# —— Helpers —— 

def get_contact_by_email(email: str) -> ContactInfo:
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email
            }]
        }],
        "properties": ["firstname","lastname","email","jobtitle"],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    print("RAW engagement response:", r.json())
    results = r.json().get("results", [])
    if not results:
        raise HTTPException(404, "Contact not found")
    c = results[0]
    p = c["properties"]
    return ContactInfo(
        id=c["id"],
        firstname=p.get("firstname",""),
        lastname=p.get("lastname",""),
        email=p.get("email",""),
        jobtitle=p.get("jobtitle")
    )

def get_company_by_domain(domain: str) -> dict:
    url = "https://api.hubapi.com/crm/v3/objects/companies/search"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "domain",
                "operator": "EQ",
                "value": domain
            }]
        }],
        "properties": [
            "name","website","industry",
            "lifecyclestage","2025_account_status"
        ],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    res = r.json().get("results", [])
    if not res:
        raise HTTPException(404, "Company not found")
    c = res[0]; p = c["properties"]
    return {
        "id": c["id"],
        "name": p.get("name"),
        "domain": domain,
        "website": p.get("website"),
        "industry": p.get("industry"),
        "lifecycle_stage": p.get("lifecyclestage"),
        "account_status": p.get("2025_account_status")
    }

from functools import lru_cache

@lru_cache()
def get_stage_label_map() -> dict:
    url = "https://api.hubapi.com/crm/v3/pipelines/deals"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    stage_map = {}
    for pipeline in data.get("results", []):
        for stage in pipeline.get("stages", []):
            stage_map[stage["id"]] = stage["label"]
    return stage_map

def get_associated_contacts(company_id: str) -> List[ContactInfo]:
    url = f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}/associations/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()

    contacts = []
    for assoc in r.json().get("results", []):
        cid = assoc["id"]
        cr = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}",
            headers=headers,
            params={"properties": "firstname,lastname,email,jobtitle"}
        )
        cr.raise_for_status()
        p = cr.json()["properties"]
        email = p.get("email")
        if not email:
            print(f"Skipping contact with missing email: {cid}")
            continue
        contacts.append(ContactInfo(
            id=cid,
            firstname=p.get("firstname") or "",
            lastname=p.get("lastname") or "",
            email=email,
            jobtitle=p.get("jobtitle") or ""
        ))

    return contacts

def get_all_deals_for_company(company_id: str) -> List[DealInfo]:
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json"
    }
    cutoff = int((datetime.utcnow() - timedelta(days=365*3)).timestamp() * 1000)
    body = {
        "filterGroups": [{
            "filters": [
                {"propertyName":"associations.company","operator":"EQ","value":company_id},
                {"propertyName":"closedate","operator":"GTE","value":cutoff}
            ]
        }],
        "properties":["dealname","amount","dealstage","closedate"],
        "limit":100
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    deals = []
    stage_map = get_stage_label_map()
    for d in r.json().get("results", []):
        p = d["properties"]
        cd = p.get("closedate")
        stage_id = p.get("dealstage", "")
        stage_label = stage_map.get(stage_id, stage_id)

        deals.append(DealInfo(
            id=d["id"],
            name=p.get("dealname",""),
            amount=float(p.get("amount",0)),
            stage=stage_label,
            closedate=isoparse(cd) if cd else None

        ))
    print(f"Fetched {len(deals)} deals for company ID {company_id}")
    for d in deals:
        print(f"  - Deal: {d.name}, Stage: {d.stage}, Closed: {d.closedate}")
    return deals

def get_recent_engagements(company_id: str, limit: int = 10) -> List[EngagementInfo]:
    print(f"Getting recent engagements for company ID: {company_id}")
    url = f"https://api.hubapi.com/engagements/v1/engagements/associated/company/{company_id}/paged?limit=100"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()

    engs = []
    count = 0
    for e in data.get("results", []):
        eng = e.get("engagement", {})
        meta = e.get("metadata", {})

        eng_type = eng.get("type", "").lower()
        if eng_type not in ["call", "email"]:
            continue

        ts = eng.get("timestamp")
        created_at = datetime.fromtimestamp(ts / 1000.0) if ts else None

        subject = meta.get("subject") or meta.get("body", "")[:50]

        engs.append(EngagementInfo(
            id=str(eng.get("id")),
            type=eng.get("type", "").title(),
            createdAt=created_at,
            subject=subject
        ))

        count += 1
        if count >= limit:
            break

    return engs


def format_engagement_summary(engs: List[EngagementInfo], limit: int = 5) -> List[str]:
    summary = []
    for e in sorted(engs, key=lambda x: x.createdAt, reverse=True)[:limit]:
        date_str = e.createdAt.strftime('%b %d, %Y') if e.createdAt else "Unknown date"
        subject = e.subject.strip() if e.subject else "No subject"
        summary.append(f"{date_str} – {e.type.title()} – {subject}")
    return summary


# —— Single “brief” endpoint —— 

@app.get("/brief", response_model=BriefResponse)
def brief(
    email:  str = Query(..., description="Contact email"),
    domain: str = Query(..., description="Firm domain, e.g. examplelaw.com")
):
    contact     = get_contact_by_email(email)
    comp_data   = get_company_by_domain(domain)
    cid         = comp_data["id"]
    contacts    = get_associated_contacts(cid)
    all_deals   = get_all_deals_for_company(cid)
    closed_won = []
    closed_lost = []
    expansion = []
    resurrection = []
    active_deals = []

    for d in all_deals:
        stage = d.stage.strip().lower()
        if stage in ("closed won", "closedwon"):
            closed_won.append(d)
        elif stage in ("closed lost", "closedlost"):
            closed_lost.append(d)
        elif stage == "expansion/renewal - won":
            expansion.append(d)
        elif stage == "resurrected account - won":
            resurrection.append(d)    
        else:
            active_deals.append(d)

    print(f"Total deals: {len(all_deals)}")
    print(f"  Closed-Won: {len(closed_won)}")
    print(f"  Closed-Lost: {len(closed_lost)}")
    print(f"  Expansion: {len(expansion)}")
    print(f"  Active: {len(active_deals)}")

    engs        = get_recent_engagements(cid)
    formatted_engagements = format_engagement_summary(engs)
    print("Formatted Engagements:\n", "\n".join(formatted_engagements))
    for e in engs:
        if not e.subject:
            e.subject = "(no subject logged)"
        e.type = e.type.title()  # e.g., "Call" instead of "CALL"
    print("Retrieved Engagements:\n", *engs, sep="\n")


    company_brief = CompanyBrief(
        id=cid,
        name=comp_data["name"],
        domain=comp_data["domain"],
        website=comp_data.get("website", ""),
        industry=comp_data.get("industry", ""),
        account_status=comp_data.get("account_status", "") or "",
        lifecycle_stage=comp_data.get("lifecycle_stage", ""),
        contacts=contacts,
        deals_closed_won=closed_won,
        deals_closed_lost=closed_lost,
        deals_expansion=expansion,
        deals_resurrected=resurrection,
        deals_active=active_deals,
        recent_engagements=engs,
        formatted_engagements=formatted_engagements
    )

    return BriefResponse(contact=contact, company=company_brief)

# —— Plugin manifest & support routes —— 

@app.get("/.well-known/ai-plugin.json")
def serve_manifest():
    return {
        "schema_version":"v1",
        "name_for_human":"HubSpot Briefing",
        "name_for_model":"hubspot_briefing",
        "description_for_human":"Fetch internal HubSpot history for a given contact & firm.",
        "description_for_model":"Call /brief to get our account status, deals, and activities.",
        "auth": {"type":"none"},
        "api": {
            "type":"openapi",
            "url":"https://hubspot-chatgpt-plugin.onrender.com/.well-known/openapi.json"
        },
        "logo_url":"https://hubspot-chatgpt-plugin.onrender.com/logo.png",
        "contact_email":"you@example.com",
        "legal_info_url":"https://hubspot-chatgpt-plugin.onrender.com/legal"
    }

@app.get("/logo.png")
def logo():
    return HTMLResponse('<img src="https://via.placeholder.com/100" alt="logo">')

@app.get("/legal", response_class=HTMLResponse)
def legal():
    return "<p>This plugin stores no data and is for internal use only.</p>"

# —— Compatibility route for ChatGPT plugin UI ——
@app.get("/openapi.json")
def openapi_redirect():
    return custom_openapi()

