import os
print("⚡️ Starting HubSpot Briefing service…")
print("HUBSPOT_TOKEN loaded?", bool(os.getenv("HUBSPOT_TOKEN")))
print("OPENAI_API_KEY loaded?", bool(os.getenv("OPENAI_API_KEY")))

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
import re
from functools import lru_cache

# —— Init & secrets —— 
DEBUG_INIT = os.getenv("DEBUG_INIT", "false").lower() == "true"
print("⚙️ DEBUG_INIT mode:", DEBUG_INIT)
load_dotenv()
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = None
if not DEBUG_INIT:
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    print("✅ OpenAI client initialized")
else:
    print("🚫 Skipping OpenAI client in DEBUG_INIT mode")

app = FastAPI(
    title="HubSpot Briefing",
    version="1.0.0",
    openapi_url="/.well-known/openapi.json",
    docs_url=None,
    redoc_url=None
)

@app.get("/")
def read_root():
    return {"status": "ok"}

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        routes=app.routes,
    )
    schema["servers"] = [
        {"url": "https://hubspot-chatgpt-plugin.onrender.com", "description": "Primary API server"}
    ]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

# —— Models —— 

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

class CompanyBrief(BaseModel):
    id: str
    name: str
    domain: str
    website: Optional[str] = None
    industry: Optional[str] = None
    account_status: Optional[str] = None
    lifecycle_stage: Optional[str] = None

    contacts: List[ContactInfo]
    deals_closed_won: List[DealInfo]
    deals_closed_lost: List[DealInfo]
    deals_expansion: List[DealInfo]
    deals_resurrected: List[DealInfo]
    deals_active: List[DealInfo]
    recent_engagements: List[EngagementInfo]
    formatted_engagements_emails: Optional[List[str]] = None
    formatted_engagements_calls: Optional[List[str]] = None

class BriefResponse(BaseModel):
    contact: ContactInfo
    company: CompanyBrief

# —— Helpers —— 

def strip_html(text: str) -> str:
    return re.sub("<[^>]+>", "", text or "").strip()

def extract_email_subject(metadata: dict) -> str:
    subj = metadata.get("subject") or metadata.get("emailSubject")
    if subj and subj.strip():
        return strip_html(subj)

    fallback = metadata.get("bodyPreview") or metadata.get("body") or metadata.get("text") or ""
    cleaned = strip_html(fallback)
    snippet = cleaned[:60]
    return snippet or "(no subject)"

def extract_call_outcome(metadata: dict) -> str:
    notes = metadata.get("body") or metadata.get("notes") or ""
    cleaned = strip_html(notes)
    return cleaned or "(no outcome logged)"

@lru_cache()
def get_stage_label_map() -> dict:
    url = "https://api.hubapi.com/crm/v3/pipelines/deals"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers); r.raise_for_status()
    stage_map = {}
    for pipeline in r.json().get("results", []):
        for stage in pipeline.get("stages", []):
            stage_map[stage["id"]] = stage["label"]
    return stage_map

def get_contact_by_email(email: str) -> ContactInfo:
    if not email:
        raise ValueError("Email must be provided to get_contact_by_email")

    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    body = {
        "filterGroups": [{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
        "properties": ["firstname","lastname","email","jobtitle"],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body); r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise HTTPException(404, "Contact not found")
    c = results[0]; p = c["properties"]
    return ContactInfo(
        id=c["id"],
        firstname=p.get("firstname","") or "",
        lastname=p.get("lastname","") or "",
        email=p.get("email"),
        jobtitle=p.get("jobtitle") or ""
    )

def get_company_by_domain(domain: str) -> dict:
    url = "https://api.hubapi.com/crm/v3/objects/companies/search"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    body = {
        "filterGroups": [{"filters":[{"propertyName":"domain","operator":"EQ","value":domain}]}],
        "properties": ["name","website","industry","lifecyclestage","2025_account_status"],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body); r.raise_for_status()
    res = r.json().get("results", [])
    if not res:
        raise HTTPException(404, "Company not found")
    c = res[0]; p = c["properties"]
    return {
        "id": c["id"],
        "name": p.get("name") or "",
        "domain": domain,
        "website": p.get("website") or "",
        "industry": p.get("industry") or "",
        "lifecycle_stage": p.get("lifecyclestage") or "",
        "account_status": p.get("2025_account_status") or ""
    }

def get_associated_contacts(company_id: str) -> List[ContactInfo]:
    url = f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}/associations/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    contacts = []
    for assoc in requests.get(url, headers=headers).json().get("results", []):
        cid = assoc["id"]
        cr = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}",
            headers=headers,
            params={"properties":"firstname,lastname,email,jobtitle"}
        ); cr.raise_for_status()
        p = cr.json()["properties"]
        email = p.get("email")
        if email:
            contacts.append(ContactInfo(
                id=cid,
                firstname=str(p.get("firstname","") or ""),
                lastname=str(p.get("lastname","") or ""),
                email=email,
                jobtitle=str(p.get("jobtitle","") or "")
            ))
    return contacts

def get_all_deals_for_company(company_id: str) -> List[DealInfo]:
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    cutoff = int((datetime.utcnow() - timedelta(days=365 * 3)).timestamp() * 1000)
    body = {
        "filterGroups": [{
            "filters": [
                {"propertyName":"associations.company","operator":"EQ","value":company_id},
                {"propertyName":"closedate","operator":"GTE","value":cutoff}
            ]
        }],
        "properties": ["dealname","amount","dealstage","closedate"],
        "limit": 100
    }
    r = requests.post(url, headers=headers, json=body); r.raise_for_status()
    deals = []
    stage_map = get_stage_label_map()
    for d in r.json().get("results", []):
        p = d["properties"]
        try:
            amt = float(str(p.get("amount","0")).replace(",", "").strip())
        except:
            amt = 0.0
        deals.append(DealInfo(
            id=d["id"],
            name=p.get("dealname","") or "",
            amount=amt,
            stage=stage_map.get(p.get("dealstage",""), p.get("dealstage","")),
            closedate=isoparse(p["closedate"]) if p.get("closedate") else None
        ))
    return deals

def get_recent_engagements(company_id: str) -> List[EngagementInfo]:
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    engs = []

    print(f"🔍 DEBUG: Pulling engagements for company ID {company_id}")

    # Company-level engagements
    url_c = f"https://api.hubapi.com/engagements/v1/engagements/associated/company/{company_id}/paged"
    offset = None
    while True:
        params = {"limit": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(url_c, headers=headers, params=params); r.raise_for_status()
        data = r.json()
        for e in data.get("results", []):
            eng = e.get("engagement", {})
            meta = e.get("metadata", {})
            t = eng.get("type","").lower()
            ts = eng.get("timestamp")
            print(f"📌 [COMPANY] ID: {eng.get('id')} TYPE: {t} TIME: {ts} META: {meta}")
            if t not in ("email", "call"):
                continue
            subject = extract_email_subject(meta) if t == "email" else extract_call_outcome(meta)
            engs.append(EngagementInfo(
                id=str(eng.get("id")),
                type=eng.get("type").title(),
                createdAt=datetime.fromtimestamp(ts/1000.0) if ts else datetime.min,
                subject=subject
            ))
        if not data.get("hasMore"):
            break
        offset = data.get("offs
