
import os
import requests
import openai
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

load_dotenv()

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = openai.OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

def get_contact(contact_email):
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
                "value": contact_email
            }]
        }],
        "properties": ["firstname", "lastname", "email", "company"],
        "limit": 1
    }
    res = requests.post(url, headers=headers, json=body)
    res.raise_for_status()
    results = res.json().get("results", [])
    if not results:
        raise Exception("No contact found.")
    return results[0]

def get_company(company_name):
    url = "https://api.hubapi.com/crm/v3/objects/companies/search"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "name",
                "operator": "EQ",
                "value": company_name
            }]
        }],
        "properties": ["name", "website", "industry", "description"],
        "limit": 1
    }
    res = requests.post(url, headers=headers, json=body)
    res.raise_for_status()
    results = res.json().get("results", [])
    return results[0] if results else {}

def summarize_contact_and_company(contact, company):
    contact_info = f"""
    Name: {contact['properties'].get('firstname')} {contact['properties'].get('lastname')}
    Email: {contact['properties'].get('email')}
    Company: {contact['properties'].get('company')}
    """
    company_info = f"""
    Company Info:
    Name: {company.get('properties', {}).get('name')}
    Website: {company.get('properties', {}).get('website')}
    Industry: {company.get('properties', {}).get('industry')}
    Description: {company.get('properties', {}).get('description')}
    """
    prompt = f"""
    Summarize the following contact and company information for a sales rep:

    {contact_info}
    {company_info}

    Include what the company does, what the contact's likely role is, and how to best approach them.
    """

    response = client.chat.completions.create(
        model="gpt-4-1106-preview",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content.strip()

@app.get("/summarize-contact")
def summarize_contact(email: str = Query(...)):
    try:
        contact = get_contact(email)
        company_name = contact["properties"].get("company", "")
        company = get_company(company_name) if company_name else {}
        summary = summarize_contact_and_company(contact, company)
        return {"summary": summary}
    except Exception as e:
        return {"error": str(e)}

@app.get("/.well-known/ai-plugin.json")
def serve_manifest():
    return {
        "schema_version": "v1",
        "name_for_human": "HubSpot Summarizer",
        "name_for_model": "hubspot_summary",
        "description_for_human": "Summarize contacts from HubSpot by email address.",
        "description_for_model": "Use this tool to retrieve and summarize contact and company information from HubSpot using an email address.",
        "auth": {
            "type": "none"
        },
        "api": {
            "type": "openapi",
            "url": "https://RENDER-URL/openapi.json"
        },
        "logo_url": "https://RENDER-URL/logo.png",
        "contact_email": "you@example.com",
        "legal_info_url": "https://RENDER-URL/legal"
    }
