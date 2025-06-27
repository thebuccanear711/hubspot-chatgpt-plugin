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

app = FastAPI(title="HubSpot Briefing", version="1.0.0",
              openapi_url="/.well-known/openapi.json", docs_url=None, redoc_url=None)

@app.get("/")
def read_root():
    return {"status": "ok"}

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version,
                         openapi_version=app.openapi_version, routes=app.routes)
    schema["servers"] = [{"url": "https://hubspot-chatgpt-plugin.onrender.com", "description": "Primary API server"}]
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

# —— HubSpot helpers —— 

def get_contact_by_email(email: str) -> ContactInfo:
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    body = {
        "filterGroups": [{"filters":[{"propertyName":"email","operator":"EQ","value": email}]}],
        "properties": ["firstname","lastname","email","jobtitle"],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body); r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise HTTPException(404, "Contact not found")
    c = results[0]; p = c["properties"]
    return ContactInfo(id=c["id"], firstname=p.get("firstname",""), lastname=p.get("lastname",""), email=p.get("email"), jobtitle=p.get("jobtitle"))

def get_company_by_domain(domain: str) -> dict:
    url = "https://api.hubapi.com/crm/v3/objects/companies/search"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    body = {
        "filterGroups":[{"filters":[{"propertyName":"domain","operator":"EQ","value":domain}]}],
        "properties": ["name","website","industry","lifecyclestage","2025_account_status"],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body); r.raise_for_status()
    res = r.json().get("results", [])
    if not res:
        raise HTTPException(404, "Company not found")
    c = res[0]; p = c["properties"]
    return {"id": c["id"], "name": p.get("name"), "domain": domain, "website": p.get("website"),
            "industry": p.get("industry"), "lifecycle_stage": p.get("lifecyclestage"),
            "account_status": p.get("2025_account_status")}

from functools import lru_cache
@lru_cache()
def get_stage_label_map() -> dict:
    url = "https://api.hubapi.com/crm/v3/pipelines/deals"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers); r.raise_for_status()
    data = r.json()
    out = {}
    for pipeline in data.get("results", []):
        for stage in pipeline.get("stages", []):
            out[stage["id"]] = stage["label"]
    return out

def get_associated_contacts(company_id: str) -> List[ContactInfo]:
    url = f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}/associations/contacts"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers); r.raise_for_status()
    contacts = []
    for assoc in r.json().get("results", []):
        cid = assoc["id"]
        cr = requests.get(f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}",
                          headers=headers, params={"properties":"firstname,lastname,email,jobtitle"})
        cr.raise_for_status()
        p = cr.json()["properties"]
        if p.get("email"):
            contacts.append(ContactInfo(
                id=cid, firstname=p.get("firstname",""), lastname=p.get("lastname",""),
                email=p.get("email"), jobtitle=p.get("jobtitle","")
            ))
    return contacts

def get_all_deals_for_company(company_id: str) -> List[DealInfo]:
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    cutoff = int((datetime.utcnow() - timedelta(days=365*3)).timestamp() * 1000)
    body = {"filterGroups":[{"filters":[{"propertyName":"associations.company","operator":"EQ","value":company_id},{"propertyName":"closedate","operator":"GTE","value":cutoff}]}],
            "properties":["dealname","amount","dealstage","closedate"], "limit":100}
    r = requests.post(url, headers=headers, json=body); r.raise_for_status()
    deals, stage_map = [], get_stage_label_map()
    for d in r.json().get("results", []):
        p = d["properties"]
        try:
            amt = float(p.get("amount", "0").replace(",", "").strip())
        except:
            amt = 0.0
        deals.append(DealInfo(
            id=d["id"], name=p.get("dealname",""), amount=amt,
            stage=stage_map.get(p.get("dealstage",""), p.get("dealstage","")),
            closedate=isoparse(p["closedate"]) if p.get("closedate") else None
        ))
    return deals

def extract_email_subject(metadata: dict) -> str:
    return (metadata.get("subject") or metadata.get("bodyPreview") or metadata.get("body") or metadata.get("text") or "").strip() or "(no subject)"

def get_recent_engagements(company_id: str) -> List[EngagementInfo]:
    url = f"https://api.hubapi.com/engagements/v1/engagements/associated/company/{company_id}/paged"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    engs, offset = [], None
    while True:
        params = {"limit":100}
        if offset: params["offset"] = offset
        r = requests.get(url, headers=headers, params=params); r.raise_for_status()
        data = r.json()
        for e in data.get("results", []):
            eng, meta = e.get("engagement", {}), e.get("metadata", {})
            t = eng.get("type","").lower()
            if t not in ("email", "call"):
                continue
            ts = eng.get("timestamp")
            engs.append(EngagementInfo(
                id=str(eng.get("id")), type=eng.get("type").title(),
                createdAt=datetime.fromtimestamp(ts/1000.0) if ts else datetime.min,
                subject=extract_email_subject(meta)
            ))
        if not data.get("hasMore"):
            break
        offset = data.get("offset")
    engs.sort(key=lambda x: x.createdAt or datetime.min, reverse=True)
    emails = [e for e in engs if e.type == "Email"][:10]
    calls  = [e for e in engs if e.type == "Call"][:10]
    return emails + calls

def format_engagement_summary(engs: List[EngagementInfo], limit: int = 5) -> dict:
    emails, calls = [], []
    cnt_e = cnt_c = 0
    for e in engs:
        if e.type == "Email" and cnt_e < limit:
            cnt_e += 1
            emails.append(f"{cnt_e}. **{e.subject}** – {e.createdAt.strftime('%b %d, %Y')}")
        elif e.type == "Call" and cnt_c < limit:
            cnt_c += 1
            calls.append(f"{cnt_c}. **Type/time:** {e.createdAt.strftime('%b %d, %Y')} – Outcome: {e.subject}")
        if cnt_e >= limit and cnt_c >= limit:
            break
    return {"emails": emails, "calls": calls}

@app.get("/brief", response_model=BriefResponse)
def brief(email: str = Query(...), domain: str = Query(...)):
    contact = get_contact_by_email(email)
    comp = get_company_by_domain(domain)
    cid = comp["id"]
    contacts = get_associated_contacts(cid)
    deals = get_all_deals_for_company(cid)
    cw, cl, exp, res, act = [], [], [], [], []
    for d in deals:
        st = d.stage.lower()
        if "closed won" in st: cw.append(d)
        elif "closed lost" in st: cl.append(d)
        elif "expansion" in st: exp.append(d)
        elif "resurrected" in st: res.append(d)
        else: act.append(d)

    engs = get_recent_engagements(cid)
    formatted = format_engagement_summary(engs)

    return BriefResponse(
        contact=contact, company=CompanyBrief(
            id=cid, name=comp["name"], domain=comp["domain"],
            website=comp.get("website",""), industry=comp.get("industry",""),
            account_status=comp.get("account_status",""), lifecycle_stage=comp.get("lifecycle_stage",""),
            contacts=contacts, deals_closed_won=cw, deals_closed_lost=cl,
            deals_expansion=exp, deals_resurrected=res, deals_active=act,
            recent_engagements=engs,
            formatted_engagements_emails=formatted["emails"],
            formatted_engagements_calls=formatted["calls"]
        )
    )

# —— Plugin metadata —— 

@app.get("/.well-known/ai-plugin.json")
def serve_manifest():
    return {
        "schema_version":"v1","name_for_human":"HubSpot Briefing",
        "name_for_model":"hubspot_briefing","description_for_human":"…",
        "description_for_model":"…","auth":{"type":"none"},
        "api":{"type":"openapi","url":"https://hubspot-chatgpt-plugin.onrender.com/.well-known/openapi.json"},
        "logo_url":"https://hubspot-chatgpt-plugin.onrender.com/logo.png","contact_email":"you@example.com",
        "legal_info_url":"https://hubspot-chatgpt-plugin.onrender.com/legal"
    }

@app.get("/logo.png")
def logo():
    return HTMLResponse('<img src="https://via.placeholder.com/100" alt="logo">')

@app.get("/legal", response_class=HTMLResponse)
def legal():
    return "<p>This plugin stores no data and is for internal use only.</p>"

@app.get("/openapi.json")
def openapi_redirect():
    return custom_openapi()
