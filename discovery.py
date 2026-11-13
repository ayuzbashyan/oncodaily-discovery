
import csv, re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests


UA = "OncoDailyDiscovery/1.0"
TIMEOUT = 18

ONCO = ("oncolog", "cancer", "tumor", "tumour", "hematolog", "haematolog", "leukemi", "lymphoma", "carcinoma", "melanoma", "radiotherap", "immunotherap", "precision medicine")
MEDIA = ("news outlet", "news publisher", "medical media", "media company", "publisher", "publishing", "magazine", "journalism", "editorial", "digital publication", "medical education", "conference organizer", "cme events", "newsletters", "journal publishing", "professional education")
MEDIUM_SPECIFIC = ("podcast", "medical news", "oncology news", "conference coverage", "expert interviews", "patient education", "clinical news", "webinar", "videos and articles", "healthcare news")
BUYER = ("pharma", "pharmaceutical", "biotech", "biotechnology", "hospital", "university", "research", "clinical", "medical center", "foundation", "association", "society", "institute", "therapeutics", "diagnostic", "healthcare", "trial", "patient advocacy")

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept": "application/json"})


def get_json(url, params=None):
    try:
        r = s.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def clean(x):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", str(x or ""))).strip()


def host(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def base_record(name, desc="", country="Unknown", website="", source="", source_url="", kind="Unknown", evidence=""):
    return {
        "name": clean(name),
        "description": clean(desc)[:600],
        "country": country or "Unknown",
        "website": website,
        "source": source,
        "source_url": source_url,
        "kind": kind or "Unknown",
        "evidence": evidence,
        "study_count": 0,
        "competition_tier": "",
        "parent_organization": "",
    }


def clinicaltrials(specialty, limit):
    data = get_json("https://clinicaltrials.gov/api/v2/studies", {
        "query.term": f"AREA[ConditionSearch]({specialty or 'cancer'})",
        "pageSize": min(100, max(20, limit * 2)),
        "format": "json",
        "sort": "LastUpdatePostDate:desc",
    })
    orgs = {}
    for study in data.get("studies", []):
        proto = study.get("protocolSection", {})
        nct = proto.get("identificationModule", {}).get("nctId", "")
        title = clean(proto.get("identificationModule", {}).get("briefTitle", ""))
        sponsor = proto.get("sponsorCollaboratorsModule", {})
        
        items = ([(sponsor["leadSponsor"], "Lead sponsor")] if sponsor.get("leadSponsor") else []) + [(x, "Collaborator") for x in sponsor.get("collaborators", [])]
        
        for org, role in items:
            name = clean(org.get("name", ""))
            if not name or name.lower() in ("unknown", "none"):
                continue
            key = re.sub(r"\W+", "", name.casefold())
            if key not in orgs:
                rec = base_record(name, f"Oncology clinical trial participation: {title}", source="ClinicalTrials.gov",
                                  source_url=f"https://clinicaltrials.gov/study/{nct}" if nct else "",
                                  kind=org.get("class", "Unknown").title(), evidence=f"{role} in {nct}")
                rec["study_count"] = 1
                orgs[key] = rec
            else:
                orgs[key]["study_count"] += 1
    return list(orgs.values())[:limit]


def openalex(specialty, limit, org_type="all"):
    params = {"search": specialty or "oncology", "per_page": min(limit, 50),
              "filter": "country_code:US" + (",type:company" if org_type == "company" else "")}
    data = get_json("https://api.openalex.org/institutions", params)
    out = []
    for item in data.get("results", []):
        cc = item.get("country_code") or (item.get("geo") or {}).get("country_code")
        if cc == "US":
            out.append(base_record(
                item.get("display_name", ""),
                f"Research organization indexed by OpenAlex; {item.get('works_count', 0)} indexed works.",
                country=cc, website=item.get("homepage_url") or "", source="OpenAlex",
                source_url=item.get("id", ""), kind=item.get("type", "Unknown"),
                evidence="Institution name matches search; this is not proof of oncology specialization."
            ))
    return out


def oncology_media_seeds(limit=25, path=None):
    csv_file = Path(path) if path else Path(__file__).resolve().parent / "data" / "us_competitors.csv"
    if not csv_file.exists():
        return []
    
    res = []
    req = ("name", "description", "website", "source_url", "parent_organization", "us_basis")
    with csv_file.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not all((row.get(k) or "").strip() for k in req):
                continue
            tier = row.get("competition_tier", "Adjacent").strip()
            if tier not in ("Direct", "Adjacent"):
                continue
            rec = base_record(row["name"], row["description"], country="US", website=row["website"],
                              source="US competitor directory", source_url=row["source_url"],
                              kind=row.get("kind", "Media / education"),
                              evidence=f"US-based publishing/operations evidence: {row['us_basis']}; confirm status.")
            rec["competition_tier"] = tier
            rec["parent_organization"] = row["parent_organization"].strip()
            res.append(rec)
    return res[:limit]


def us_location_evidence(rec):
    cc = str(rec.get("country", "")).strip().upper()
    return "Structured country code: US" if cc == "US" else None


def score_record(rec, specialty, preferred_type):
    txt = f"{rec.get('name', '')} {rec.get('description', '')} {rec.get('kind', '')}".lower()
    onco_hit = any(t in txt for t in ONCO)
    spec_hit = bool(specialty and specialty.strip().lower() in txt)
    media_hit = any(t in txt for t in MEDIA) or (any(t in txt for t in MEDIUM_SPECIFIC) and any(x in txt for x in ("coverage", "publishes", "publication", "editorial", "media")))
    buyer_hit = any(t in txt for t in BUYER)
    src = rec["source"]

    pts = min(100, (30 if onco_hit else 0) + (20 if spec_hit else 0) + (20 if (media_hit or buyer_hit or src == "ClinicalTrials.gov") else 0) + 
              (15 if src == "ClinicalTrials.gov" else 10 if src == "OpenAlex" else 5) + (10 if rec.get("website") else 0) + (5 if rec.get("study_count", 0) >= 2 else 0))

    tier = rec.get("competition_tier", "")
    if src == "US competitor directory":
        label = "Potential competitor"
        tier = tier if tier in ("Direct", "Adjacent") else "Adjacent"
        rationale = f"{tier} overlap in oncology media/education. Brand ownership needs review."
        signals = ("news", "clinical", "articles", "interviews", "videos", "podcasts", "conference", "events", "education", "magazine", "journal", "publishing")
        pts = min(95, (66 if tier == "Direct" else 48) + (9 if onco_hit else 0) + (5 if "media" in rec.get("kind", "").lower() else 0) + (4 if rec.get("website") else 0) + min(12, 2 * sum(t in txt for t in signals)))
    elif src in ("ClinicalTrials.gov", "OpenAlex"):
        label = "Potential customer" if (onco_hit or spec_hit) else "Review required"
        rationale = "Sponsor or academic lead; commercial prospect, not a direct publisher."
    elif onco_hit and media_hit and not (buyer_hit and not any(t in txt for t in MEDIA)):
        label, tier = "Potential competitor", "Direct"
        rationale = "Signals match oncology publishing or events business."
    elif buyer_hit and onco_hit:
        label = "Potential customer"
        rationale = "Oncology commercial entity; verify business fit."
    else:
        label = "Review required"
        rationale = "Weak business signals; needs manual review."

    if preferred_type != "Both" and label != preferred_type:
        return None

    return {**rec, "category": label, "relevance_score": pts, "rationale": rationale,
            "competition_tier": tier if label == "Potential competitor" else "",
            "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def dedupe(rows):
    unique = {}
    for r in rows:
        norm = re.sub(r"[^a-z0-9]", "", r.get("name", "").casefold())
        if not norm:
            continue
        src = r.get("source")
        key = f"brand:{norm}:{host(r.get('website', ''))}" if src == "US competitor directory" else f"org:{host(r.get('website', '')) or norm}"
        if key not in unique or (src == "ClinicalTrials.gov" and unique[key].get("source") != "ClinicalTrials.gov"):
            unique[key] = r
    return list(unique.values())