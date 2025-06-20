import os
import requests
import openai
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List
from datetime import datetime, timedelta
from dotenv import load_dotenv

# —— Load secrets —— 
load_dotenv()
HUBSPOT_TOKEN   = os.getenv("HUBSPOT_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
client          = openai.OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

# —— Data models —— 

class ContactInfo(BaseModel):
    id: str
    firstname: str
    lastname: str
    email: str
    jobtitle: str = None

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
    website: str = None
    industry: str = None
    account_status: str = None
    lifecycle_stage: str = None

    contacts:           List[ContactInfo]
    deals_closed_won:   List[DealInfo]
    deals_closed_lost:  List[DealInfo]
    deals_expansion:    List[DealInfo]
    deals_active:       List[DealInfo]
    recent_engagements: List[EngagementInfo]

class BriefResponse(BaseModel):
    contact: ContactInfo
    company: CompanyBrief

# —— Helpers —— 

def get_contact_by_email(email: str):
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "filterGroups": [{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}],
        "properties": ["firstname","lastname","email","jobtitle"],
        "limit": 1
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
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

def get_company_by_domain(domain: str):
    url = "https://api.hubapi.com/crm/v3/objects/companies/search"
    headers = {"Authorization":f"Bearer {HUBSPOT_TOKEN}","Content-Type":"application/json"}
    body = {
        "filterGroups":[{"filters":[{"propertyName":"domain","operator":"EQ","value":domain}]}],
        "properties":["name","website","industry","lifecyclestage","2025_account_status"],
        "limit":1
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    res = r.json().get("results", [])
    if not res:
        raise HTTPException(404, "Company not found")
    c = res[0]; p = c["properties"]
    return {
        "id":              c["id"],
        "name":            p.get("name"),
        "domain":          domain,
        "website":         p.get("website"),
        "industry":        p.get("industry"),
        "lifecycle_stage": p.get("lifecyclestage"),
        "account_status":  p.get("2025_account_status")
    }

def get_associated_contacts(company_id: str):
    url = f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}/associations/contacts"
    headers = {"Authorization":f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    contacts = []
    for assoc in r.json().get("results", []):
        cid = assoc["id"]
        cr = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}",
            headers=headers,
            params={"properties":"firstname,lastname,email,jobtitle"}
        )
        cr.raise_for_status()
        p = cr.json()["properties"]
        contacts.append(ContactInfo(
            id=cid,
            firstname=p.get("firstname",""),
            lastname=p.get("lastname",""),
            email=p.get("email",""),
            jobtitle=p.get("jobtitle")
        ))
    return contacts

def get_all_deals_for_company(company_id: str):
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    headers = {"Authorization":f"Bearer {HUBSPOT_TOKEN}", "Content-Type":"application/json"}
    # past 3 years:
    cutoff = int((datetime.utcnow() - timedelta(days=365*3)).timestamp()*1000)
    body = {
        "filterGroups":[{"filters":[
            {"propertyName":"associations.company","operator":"EQ","value":company_id},
            {"propertyName":"closedate","operator":"GTE","value":cutoff}
        ]}],
        "properties":["dealname","amount","dealstage","closedate"],
        "limit":100
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    deals = []
    for d in r.json().get("results",[]):
        p = d["properties"]
        cd = p.get("closedate")
        deals.append(DealInfo(
            id=d["id"],
            name=p.get("dealname",""),
            amount=float(p.get("amount",0)),
            stage=p.get("dealstage",""),
            closedate=datetime.fromtimestamp(int(cd)/1000) if cd else None
        ))
    return deals

def get_recent_engagements(company_id: str, limit:int=10):
    url = "https://api.hubapi.com/crm/v3/objects/engagements/search"
    headers = {"Authorization":f"Bearer {HUBSPOT_TOKEN}", "Content-Type":"application/json"}
    body = {
        "filterGroups":[{"filters":[
            {"propertyName":"associations.company","operator":"EQ","value":company_id},
            {"propertyName":"engagement.type","operator":"IN","value":["CALL","MEETING","EMAIL"]}
        ]}],
        "properties":["engagement.type","createdAt","metadata.subject"],
        "sorts":["-createdAt"],
        "limit":limit
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    engs = []
    for e in r.json().get("results",[]):
        p = e["properties"]
        engs.append(EngagementInfo(
            id=e["id"],
            type=p.get("engagement.type",""),
            createdAt=datetime.fromisoformat(p.get("createdAt")),
            subject=p.get("metadata.subject")
        ))
    return engs

# —— Single “brief” endpoint —— 

@app.get("/brief", response_model=BriefResponse)
def brief(
    email:  str = Query(..., description="Contact email"),
    domain: str = Query(..., description="Firm domain, e.g. examplelaw.com")
):
    # 1) Contact
    contact = get_contact_by_email(email)

    # 2) Company internal history
    comp_data = get_company_by_domain(domain)
    cid = comp_data["id"]

    # 3) Contacts at firm
    contacts = get_associated_contacts(cid)

    # 4) Deals
    all_deals    = get_all_deals_for_company(cid)
    closed_won   = [d for d in all_deals if d.stage=="closedwon"]
    closed_lost  = [d for d in all_deals if d.stage=="closedlost"]
    expansion    = [d for d in all_deals if d.stage=="expansion"]
    active_deals = [d for d in all_deals if d.stage not in ("closedwon","closedlost","expansion")]

    # 5) Recent activity
    engs = get_recent_engagements(cid)

    company_brief = CompanyBrief(
        id=cid,
        name=comp_data["name"],
        domain=comp_data["domain"],
        website=comp_data["website"],
        industry=comp_data["industry"],
        account_status=comp_data["account_status"],
        lifecycle_stage=comp_data["lifecycle_stage"],
        contacts=contacts,
        deals_closed_won=closed_won,
        deals_closed_lost=closed
