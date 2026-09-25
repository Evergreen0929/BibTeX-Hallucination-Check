#!/usr/bin/env python3
"""Strict field-level BibTeX hallucination checker.

The original Crossref -> arXiv -> official-web pipeline is retained.  Crossref
and arXiv now return multiple structured candidates, OpenAlex and publisher
URLs provide independent evidence, and Codex replaces the former Qwen API only
as the final semantic judge.  The input BibTeX is always opened read-only.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path

import bibtexparser
import requests
from ddgs import DDGS


# ============================================================================
# 1. Configuration
# ============================================================================
MODEL_NAME = "Codex"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
CODEX_SCHEMA = Path(__file__).with_name("codex_upstream_schema.json")
CROSSREF_EMAIL = os.environ.get("CROSSREF_EMAIL", "your@email.com")
LLM_AVAILABLE = False

OFFICIAL_DOMAINS = [
    "arxiv.org", "ieee.org", "thecvf.com", "cvfoundation.org", "ecva.net",
    "acm.org", "openreview.net", "proceedings.iclr.cc", "springer.com",
    "neurips.cc", "icml.cc", "aaai.org", "ijcai.org", "wiley.com",
    "onlinelibrary.wiley.com", "aclanthology.org", "openai.com",
    "deploymentsafety.openai.com", "pnas.org", "nature.com", "science.org",
]

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
ARXIV_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?")

VENUE_ALIASES = {
    "cvpr": ("cvpr", "computer vision and pattern recognition"),
    "iccv": ("iccv", "international conference on computer vision"),
    "eccv": ("eccv", "european conference on computer vision"),
    "iclr": ("iclr", "international conference on learning representations"),
    "neurips": ("neurips", "neural information processing systems"),
    "icra": ("icra", "international conference on robotics and automation"),
    "3dv": ("3dv", "international conference on 3d vision"),
    "aaai": ("aaai", "association for the advancement of artificial intelligence"),
    "acmmm": ("acm multimedia", "acm international conference on multimedia"),
    "cgf": ("computer graphics forum",),
    "tpami": ("pattern analysis and machine intelligence", "tpami"),
    "acl": ("annual meeting of the association for computational linguistics", "acl"),
    "accv": ("asian conference on computer vision", "accv"),
    "wacv": ("winter conference on applications of computer vision", "wacv"),
}


def check_llm_status():
    global LLM_AVAILABLE
    LLM_AVAILABLE = Path(CODEX_BIN).is_file() and CODEX_SCHEMA.is_file()
    print("Codex semantic judge available." if LLM_AVAILABLE else "Codex unavailable; ambiguous entries cannot be promoted.")


# ============================================================================
# 2. Normalization and comparison
# ============================================================================
def clean_latex(text):
    if not text:
        return ""
    for command, replacement in (
        (r"\pi", "pi"), (r"\Pi", "Pi"), (r"\Omega", "Omega"), (r"\omega", "omega"),
        (r"\ss", "ss"), (r"\ae", "ae"), (r"\AE", "AE"), (r"\oe", "oe"), (r"\OE", "OE"),
        (r"\o", "o"), (r"\O", "O"), (r"\l", "l"), (r"\L", "L"), (r"\aa", "aa"), (r"\AA", "AA"),
    ):
        text = text.replace(command, replacement)
    text = re.sub(r"\\(?:text|mathrm|mathbf|mathit|emph)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:['\"`^~=.]|[vHuck])\s*\{?([A-Za-z])\}?", r"\1", text)
    text = text.replace("π", "pi").replace("Π", "Pi")
    text = text.replace("Ω", "Omega").replace("ω", "omega")
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    return re.sub(r"[{}$\\]", "", text).strip()


def normalize_text(text):
    value = clean_latex(text or "").replace("ß", "ss").replace("ẞ", "SS")
    value = re.sub(r"[‐‑‒–—―−]", "-", value)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def title_similarity(left, right):
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


def title_matches(left, right):
    a, b = normalize_text(left), normalize_text(right)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.985)


def split_bib_authors(author_field):
    authors = []
    for raw in (author_field or "").split(" and "):
        raw = raw.strip()
        if not raw:
            continue
        if raw.lower() == "others":
            authors.append("others")
        else:
            authors.append(raw)
    return authors


def author_family(name):
    if "," in (name or ""):
        return normalize_text(name.split(",", 1)[0])
    tokens = normalize_text(name).split()
    if not tokens:
        return ""
    particles = {"van", "von", "de", "del", "der", "di", "da", "le"}
    family = [tokens[-1]]
    index = len(tokens) - 2
    while index >= 0 and tokens[index] in particles:
        family.insert(0, tokens[index])
        index -= 1
    return " ".join(family)


def author_name_matches(bib_name, candidate_name):
    """Compare one author without losing compound family names or initials."""
    if not bib_name or not candidate_name:
        return False
    if "," in bib_name:
        bib_family, bib_given = [part.strip() for part in bib_name.split(",", 1)]
        family = normalize_text(bib_family)
        expected_given = normalize_text(bib_given).split()
    else:
        normalized = normalize_text(bib_name)
        family = author_family(bib_name)
        expected_given = normalized[: -len(family)].strip().split()

    if "," in candidate_name:
        observed_family, observed_given_raw = [part.strip() for part in candidate_name.split(",", 1)]
        if normalize_text(observed_family) != family:
            return False
        observed_given = normalize_text(observed_given_raw).split()
    else:
        observed = normalize_text(candidate_name)
        if not (observed == family or observed.endswith(" " + family)):
            return False
        observed_given = observed[: -len(family)].strip().split()
    particles = {"van", "von", "de", "del", "der", "di", "da", "le"}
    if len(family.split()) == 1 and observed_given and observed_given[-1] in particles:
        return False
    if not expected_given or not observed_given:
        return not expected_given and not observed_given
    # Full names and bibliographic metadata frequently differ only in omitted
    # middle names or initials.  Require the same family name and compatible
    # first given name; middle-name expansion/contraction does not alter identity.
    return expected_given[0] == observed_given[0] or expected_given[0][:1] == observed_given[0][:1]


def authors_match(bib_author_field, candidate_authors):
    bib_authors = split_bib_authors(bib_author_field)
    candidate_authors = candidate_authors or []
    if not bib_authors or not candidate_authors:
        return None
    truncated = bib_authors[-1] == "others"
    compared = bib_authors[:-1] if truncated else bib_authors
    if (not truncated and len(compared) != len(candidate_authors)) or len(compared) > len(candidate_authors):
        return False
    return all(
        author_name_matches(expected, observed)
        for expected, observed in zip(compared, candidate_authors[: len(compared)])
    )


def bib_venue(entry):
    return entry.get("booktitle") or entry.get("journal") or ""


def venue_code(value):
    normalized = normalize_text(value)
    if not normalized:
        return ""
    if "arxiv" in normalized:
        return "arxiv"
    for code, aliases in VENUE_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return code
    return normalized


def venue_matches(left, right):
    if not left or not right:
        return None
    left_text, right_text = normalize_text(left), normalize_text(right)
    for qualifier in ("workshop", "findings"):
        if (qualifier in left_text) != (qualifier in right_text):
            return False
    a, b = venue_code(left), venue_code(right)
    return a == b or a in b or b in a


def type_matches(entry, candidate):
    source = candidate.get("source")
    work_type = normalize_text(candidate.get("type", ""))
    venue = venue_code(candidate.get("venue", ""))
    bib_type = entry.get("ENTRYTYPE", "").lower()
    if source == "arXiv" or venue == "arxiv":
        return bib_type in {"article", "misc"}
    if not work_type:
        return None
    if bib_type == "inproceedings":
        return "proceedings" in work_type or venue in VENUE_ALIASES
    if bib_type == "article":
        return "journal" in work_type or venue in {"cgf", "tpami", "arxiv", "neurips"}
    if bib_type == "misc":
        return True
    return None


def extract_arxiv_id(entry):
    joined = " ".join(entry.get(field, "") for field in ("url", "journal", "booktitle", "note", "eprint"))
    match = ARXIV_RE.search(joined)
    return match.group(1) if match else None


def normalize_doi(value):
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.rstrip("/ .")


def get_core_words(title):
    value = re.sub(r"[^a-zA-Z0-9\s]", " ", clean_latex(title)).lower()
    stop = {"and", "for", "the", "with", "from", "using", "based", "towards", "of", "in", "on", "a", "an"}
    return [word for word in value.split() if len(word) > 2 and word not in stop]


def get_raw_bibtex(entry):
    result = f"@{entry.get('ENTRYTYPE', 'article')}{{{entry.get('ID', 'key')},\n"
    for key, value in entry.items():
        if key not in ("ENTRYTYPE", "ID"):
            result += f"  {key} = {{{value}}},\n"
    return result + "}"


# ============================================================================
# 3. Codex semantic judge (transport replacement for Qwen)
# ============================================================================
def build_verification_prompt(entry, search_info):
    return f"""You are an expert academic citation auditor. Determine whether the BibTeX entry and the official search result refer to the exact same work. Do not accept a merely related paper.

[BibTeX]
Title: {entry.get('title', 'N/A')}
Authors in order: {entry.get('author', 'N/A')}
Year: {entry.get('year', 'N/A')}
Entry type: {entry.get('ENTRYTYPE', 'N/A')}
Venue/status: {bib_venue(entry) or 'N/A'}
DOI/arXiv/URL: {entry.get('doi') or entry.get('eprint') or entry.get('url') or 'N/A'}

[Official search evidence]
{search_info}

Check the complete normalized title, title changes between preprint and proceedings, full author list and order, whether an 'and others' prefix is correct, year, work type, final venue versus arXiv-only status, DOI/arXiv/URL identity, and consistency among sources. Return is_match=true only for the exact same work. Mention every visible discrepancy.

Return JSON: {{"is_match": true/false, "reason": "concise field-level explanation"}}"""


def llm_verify_paper(entry, search_info):
    if not LLM_AVAILABLE:
        return {"is_match": False, "reason": "Codex unavailable"}
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as handle:
        output = Path(handle.name)
    try:
        process = subprocess.run(
            [
                CODEX_BIN, "exec", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "--sandbox", "read-only",
                "--output-schema", str(CODEX_SCHEMA),
                "--output-last-message", str(output), "-",
            ],
            input=build_verification_prompt(entry, search_info),
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if process.returncode != 0:
            return {"is_match": False, "reason": f"Codex error: {process.stderr[-500:]}"}
        return json.loads(output.read_text())
    except Exception as error:
        return {"is_match": False, "reason": f"Codex error: {error}"}
    finally:
        output.unlink(missing_ok=True)


# ============================================================================
# 4. Multiple-candidate retrieval
# ============================================================================
def crossref_candidates(entry, session):
    params = {
        "query.title": clean_latex(entry.get("title", "")),
        "query.author": split_bib_authors(entry.get("author", ""))[0] if entry.get("author") else "",
        "rows": 5,
        "mailto": CROSSREF_EMAIL,
    }
    response = session.get("https://api.crossref.org/works", params=params, timeout=20)
    response.raise_for_status()
    output = []
    for item in response.json().get("message", {}).get("items", []):
        date = item.get("published") or item.get("issued") or {}
        parts = date.get("date-parts") or [[None]]
        containers = item.get("container-title") or []
        venue = next(
            (container for container in containers if venue_code(container) in VENUE_ALIASES),
            containers[0] if containers else "",
        )
        venue_year = re.search(r"(?:19|20)\d{2}", venue)
        year = venue_year.group(0) if venue_year and venue_code(venue) in VENUE_ALIASES else (
            str(parts[0][0]) if parts and parts[0] and parts[0][0] else ""
        )
        work_type = item.get("type", "")
        if work_type == "book-chapter" and venue_code(venue) in VENUE_ALIASES:
            work_type = "proceedings-article"
        output.append({
            "source": "Crossref",
            "title": (item.get("title") or [""])[0],
            "authors": [" ".join(part for part in (a.get("given", ""), a.get("family", "")) if part) for a in item.get("author", [])],
            "year": year,
            "venue": venue,
            "type": work_type,
            "doi": item.get("DOI", ""),
            "url": item.get("URL", ""),
        })
    return output


def openalex_candidates(entry, session):
    params = {
        "search": clean_latex(entry.get("title", "")),
        "per-page": 5,
        "mailto": CROSSREF_EMAIL,
        "select": "id,title,authorships,publication_year,primary_location,doi,type",
    }
    response = session.get("https://api.openalex.org/works", params=params, timeout=20)
    response.raise_for_status()
    output = []
    for item in response.json().get("results", []):
        source = ((item.get("primary_location") or {}).get("source") or {})
        output.append({
            "source": "OpenAlex",
            "title": item.get("title", ""),
            "authors": [(a.get("author") or {}).get("display_name", "") for a in item.get("authorships", [])],
            "year": str(item.get("publication_year") or ""),
            "venue": source.get("display_name", ""),
            "type": item.get("type", ""),
            "doi": item.get("doi", "") or "",
            "url": item.get("id", ""),
        })
    return output


def arxiv_candidates(entry, session):
    identifier = extract_arxiv_id(entry)
    if identifier:
        params = {"id_list": identifier, "max_results": 3}
    else:
        words = get_core_words(entry.get("title", ""))[:8]
        params = {"search_query": " AND ".join(f"all:{word}" for word in words), "max_results": 10}
    response = session.get("https://export.arxiv.org/api/query", params=params, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    if not identifier and not root.findall("atom:entry", ATOM_NS):
        authors = split_bib_authors(entry.get("author", ""))
        family = author_family(authors[0]) if authors else ""
        words = get_core_words(entry.get("title", ""))[:2]
        query_parts = ([f'au:"{family}"'] if family else []) + [f"all:{word}" for word in words]
        response = session.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": " AND ".join(query_parts), "max_results": 20},
            timeout=30,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    output = []
    for node in root.findall("atom:entry", ATOM_NS):
        node_id = node.findtext("atom:id", "", ATOM_NS)
        match = ARXIV_RE.search(node_id)
        output.append({
            "source": "arXiv",
            "title": " ".join(node.findtext("atom:title", "", ATOM_NS).split()),
            "authors": [a.findtext("atom:name", "", ATOM_NS) for a in node.findall("atom:author", ATOM_NS)],
            "year": node.findtext("atom:published", "", ATOM_NS)[:4],
            "venue": "arXiv",
            "type": "preprint",
            "doi": node.findtext("arxiv:doi", "", ATOM_NS),
            "url": node_id,
            "arxiv_id": match.group(1) if match else "",
            "journal_ref": node.findtext("arxiv:journal_ref", "", ATOM_NS),
        })
    return output


def publisher_candidate(entry, session):
    url = entry.get("url", "")
    if not url and entry.get("doi"):
        url = f"https://doi.org/{normalize_doi(entry['doi'])}"
    if not url:
        return None
    if "arxiv.org/html/" in url:
        url = url.replace("arxiv.org/html/", "arxiv.org/abs/")
    try:
        structured = official_page_candidate(url, session)
        if structured.get("title"):
            return structured
    except Exception:
        pass
    response = session.get(url, timeout=20)
    response.raise_for_status()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.I | re.S)
    page_title = html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))).strip() if title_match else ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", response.text))
    return {
        "source": "Publisher URL",
        "title": page_title,
        "authors": [],
        "year": "",
        "venue": "",
        "type": "official artifact",
        "doi": "",
        "url": response.url,
        "snippet": re.sub(r"\s+", " ", text)[:5000],
    }


class CitationMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        attributes = {key.lower(): value for key, value in attrs if key and value}
        name = (attributes.get("name") or attributes.get("property") or "").lower()
        content = attributes.get("content", "").strip()
        if name and content:
            self.meta.setdefault(name, []).append(html.unescape(content))


def official_page_candidate(url, session):
    if "arxiv.org/html/" in url:
        url = url.replace("arxiv.org/html/", "arxiv.org/abs/")
    response = session.get(url, timeout=25)
    response.raise_for_status()
    parser = CitationMetaParser()
    parser.feed(response.text)
    metadata = parser.meta
    title = (metadata.get("citation_title") or metadata.get("og:title") or [""])[0]
    authors = metadata.get("citation_author", [])
    venue = (
        metadata.get("citation_conference_title")
        or metadata.get("citation_journal_title")
        or metadata.get("citation_inbook_title")
        or [""]
    )[0]
    if re.search(r"/content/CVPR\d{4}F/", response.url, flags=re.I):
        venue = (venue + " Findings").strip()
    elif re.search(r"/content/(?:ICCV|CVPR)\d{4}W/", response.url, flags=re.I):
        venue = (venue + " Workshops").strip()
    date = (
        metadata.get("citation_publication_date")
        or metadata.get("citation_date")
        or metadata.get("citation_online_date")
        or [""]
    )[0]
    year_match = re.search(r"(?:19|20)\d{2}", date)
    inbook = (metadata.get("citation_inbook_title") or [""])[0]
    conference_year = re.search(r"(?:19|20)\d{2}", inbook) if venue else None
    doi = (metadata.get("citation_doi") or [""])[0]
    if not title:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.I | re.S)
        title = html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))).strip() if title_match else ""
    candidate = {
        "source": "Official page",
        "title": title,
        "authors": authors,
        "year": conference_year.group(0) if conference_year else (year_match.group(0) if year_match else ""),
        "venue": venue,
        "type": "proceedings-article" if venue_code(venue) in VENUE_ALIASES else ("journal-article" if venue else ""),
        "doi": doi,
        "url": response.url,
    }
    arxiv_match = ARXIV_RE.search(response.url)
    if "arxiv.org/" in response.url and arxiv_match:
        candidate.update({
            "venue": "arXiv",
            "type": "preprint",
            "arxiv_id": arxiv_match.group(1),
        })
    return candidate


def doi_official_candidates(entry, candidates, session):
    output, seen = [], set()
    for candidate in candidates:
        doi = (candidate.get("doi") or "").removeprefix("https://doi.org/")
        if not doi or doi in seen or not title_matches(entry.get("title", ""), candidate.get("title", "")):
            continue
        seen.add(doi)
        try:
            output.append(official_page_candidate(f"https://doi.org/{doi}", session))
        except Exception:
            continue
    return output


def claimed_venue_candidate(entry, session):
    """Resolve a paper through the claimed conference's official proceedings index."""
    code = venue_code(bib_venue(entry))
    year = entry.get("year", "")
    indexes = []
    if code == "iclr" and year:
        indexes.append(f"https://proceedings.iclr.cc/paper_files/paper/{year}")
    elif code in {"cvpr", "iccv"} and year:
        conference = code.upper()
        indexes.append(f"https://openaccess.thecvf.com/{conference}{year}?day=all")
        if code == "cvpr":
            indexes.append(f"https://openaccess.thecvf.com/{conference}{year}_findings?day=all")
    if not indexes:
        return None

    for index_url in indexes:
        response = session.get(index_url, timeout=30)
        response.raise_for_status()
        for href, inner in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", response.text, flags=re.I | re.S):
            anchor = html.unescape(re.sub(r"<[^>]+>", " ", inner))
            if title_matches(entry.get("title", ""), anchor):
                return official_page_candidate(urllib.parse.urljoin(index_url, href), session)
    return None


# ============================================================================
# 5. Field aggregation and three final categories
# ============================================================================
def evaluate(entry, candidate):
    item = dict(candidate)
    item["checks"] = {
        "title": title_matches(entry.get("title", ""), item.get("title", "")),
        "authors": authors_match(entry.get("author", ""), item.get("authors", [])),
        "year": str(entry.get("year")) == str(item.get("year")) if entry.get("year") and item.get("year") else None,
        "type": type_matches(entry, item),
        "venue": venue_matches(bib_venue(entry), item.get("venue", "")) if bib_venue(entry) and item.get("venue") else None,
        "doi": normalize_doi(entry.get("doi")) == normalize_doi(item.get("doi")) if entry.get("doi") and item.get("doi") else None,
        "arxiv": extract_arxiv_id(entry) == item.get("arxiv_id") if extract_arxiv_id(entry) and item.get("arxiv_id") else None,
    }
    item["title_similarity"] = round(title_similarity(entry.get("title", ""), item.get("title", "")), 4)
    return item


def same_work(candidate):
    checks = candidate["checks"]
    return checks["title"] and checks["authors"] is not False


def aggregate(entry, candidates):
    identities = [candidate for candidate in candidates if same_work(candidate)]
    if not identities:
        return False, [], ["no exact title/author identity match"]

    fields = {
        "title": True,
        "authors": any(c["checks"]["authors"] is True for c in identities),
        "year": (not entry.get("year")) or any(c["checks"]["year"] is True for c in identities),
        "type": any(c["checks"]["type"] is True for c in identities),
        "venue": True,
        "identifier": True,
        "publication status": True,
    }

    venue = bib_venue(entry)
    if venue_code(venue) == "arxiv":
        identifier = extract_arxiv_id(entry)
        fields["venue"] = any(
            c["source"] == "arXiv" or venue_code(c.get("venue")) == "arxiv"
            for c in identities
        )
        fields["identifier"] = bool(identifier) and any(c.get("arxiv_id") == identifier for c in identities)
        current_arxiv = [
            candidate for candidate in candidates
            if candidate.get("source") == "arXiv"
            and (not identifier or candidate.get("arxiv_id") == identifier)
        ]
        if current_arxiv:
            fields["current arXiv metadata"] = any(
                candidate["checks"]["title"]
                and candidate["checks"]["authors"] is True
                and candidate["checks"]["year"] is not False
                for candidate in current_arxiv
            )
        final_versions = [
            candidate for candidate in identities
            if candidate.get("venue")
            and venue_code(candidate.get("venue")) != "arxiv"
            and normalize_text(candidate.get("type")) not in {"posted content", "preprint"}
        ]
        fields["publication status"] = not final_versions
    elif venue:
        fields["venue"] = any(c["checks"]["venue"] is True for c in identities)

    if entry.get("doi"):
        fields["identifier"] = any(c["checks"]["doi"] is True for c in identities)

    def coherent(candidate):
        checks = candidate["checks"]
        if not checks["title"] or checks["authors"] is not True:
            return False
        if entry.get("year") and checks["year"] is not True:
            return False
        if entry.get("ENTRYTYPE") and checks["type"] is not True:
            return False
        if venue and checks["venue"] is not True:
            return False
        if entry.get("doi") and checks["doi"] is not True:
            return False
        if venue_code(venue) == "arxiv":
            identifier = extract_arxiv_id(entry)
            if not identifier or candidate.get("arxiv_id") != identifier:
                return False
            if candidate.get("source") != "arXiv":
                return False
        return True

    coherent_candidates = [candidate for candidate in candidates if coherent(candidate)]
    if not coherent_candidates:
        fields["single-source consistency"] = False

    # A conflicting record for the same claimed publication version prevents a
    # silent Verified decision even if another aggregator happens to agree.
    relevant_version = [
        candidate for candidate in candidates
        if candidate["checks"]["title"]
        and candidate["checks"]["type"] is True
        and (not venue or candidate["checks"]["venue"] is True)
        and (
            venue_code(venue) != "arxiv"
            or candidate.get("source") == "arXiv"
            or "arxiv.org" in candidate.get("url", "")
        )
    ]
    if coherent_candidates and any(
        candidate["checks"]["authors"] is False
        or (entry.get("year") and candidate["checks"]["year"] is False)
        for candidate in relevant_version
    ):
        fields["source consistency"] = False

    problems = [name for name, passed in fields.items() if not passed]
    return not problems, identities, problems


def cross_source_warnings(candidates):
    exact = [candidate for candidate in candidates if candidate["checks"].get("title") is True]
    warnings = []
    for field in ("authors", "year", "type", "venue"):
        values = [candidate["checks"].get(field) for candidate in exact]
        if True in values and False in values:
            warnings.append(f"{field} differs across retrieved sources")
    return warnings


def confirmed_problem_names(problems, candidates):
    exact = [candidate for candidate in candidates if candidate["checks"].get("title") is True]
    check_name = {"authors": "authors", "year": "year", "type": "type", "venue": "venue"}
    output = []
    for problem in problems:
        check = check_name.get(problem)
        if check and any(candidate["checks"].get(check) is True for candidate in exact):
            continue
        output.append(problem)
    return output


def make_record(entry, category, reason, candidates, warnings=None):
    return {
        "key": entry.get("ID"),
        "title": entry.get("title", ""),
        "category": category,
        "reason": reason,
        "warnings": warnings or [],
        "bib_fields": {
            "authors": entry.get("author", ""),
            "year": entry.get("year", ""),
            "entry_type": entry.get("ENTRYTYPE", ""),
            "venue": bib_venue(entry),
            "doi": entry.get("doi", ""),
            "arxiv_id": extract_arxiv_id(entry) or "",
            "url": entry.get("url", ""),
        },
        "sources": candidates[:6],
        "bib": get_raw_bibtex(entry),
    }


def official_artifact_candidate(entry, candidates):
    if entry.get("ENTRYTYPE") != "misc":
        return None
    corporate = normalize_text(entry.get("author", ""))
    bib_title = normalize_text(entry.get("title", ""))
    for candidate in candidates:
        host = urllib.parse.urlparse(candidate.get("url", "")).netloc.lower()
        candidate_title = normalize_text(candidate.get("title", ""))
        if (
            host and corporate and corporate in normalize_text(host)
            and bib_title and (bib_title in candidate_title or candidate_title in bib_title)
        ):
            return candidate
    return None


def official_web_candidates(entry, ddgs, session):
    structured = []
    semantic = []
    venue_hint = bib_venue(entry)
    domain_hints = {
        "iclr": "proceedings.iclr.cc",
        "cvpr": "openaccess.thecvf.com",
        "iccv": "openaccess.thecvf.com",
        "eccv": "ecva.net",
        "neurips": "neurips.cc",
        "acl": "aclanthology.org",
        "cgf": "onlinelibrary.wiley.com",
    }
    claimed_domain = domain_hints.get(venue_code(venue_hint))
    queries = [
        f'site:{claimed_domain} "{clean_latex(entry.get("title", ""))}"' if claimed_domain else "",
        f'"{clean_latex(entry.get("title", ""))}" {venue_hint} {entry.get("year", "")}'.strip(),
        clean_latex(entry.get("title", "")),
    ]
    results, seen = [], set()
    for query in queries:
        if not query:
            continue
        for result in ddgs.text(query, max_results=10):
            url = result.get("href", "")
            if url and url not in seen:
                seen.add(url)
                results.append(result)

    fallback_evidence = []
    for result in results:
        url = result.get("href", "")
        if not any(domain in url.lower() for domain in OFFICIAL_DOMAINS):
            continue
        try:
            page = official_page_candidate(url, session)
            if page.get("title"):
                structured.append(page)
                if title_matches(entry.get("title", ""), page.get("title", "")):
                    return structured, semantic
        except Exception:
            pass
        fallback_evidence.append((
            f"Title: {result.get('title')} | Snippet: {result.get('body')} | URL: {url}",
            result.get("title", ""),
            url,
        ))

    for evidence, result_title, result_url in fallback_evidence:
        judgment = llm_verify_paper(entry, evidence)
        if judgment.get("is_match"):
            semantic.append({
                "source": "Official web",
                "title": result_title,
                "authors": [], "year": "", "venue": "", "type": "", "doi": "", "url": result_url,
                "checks": {"title": title_matches(entry.get("title", ""), result_title), "authors": None, "year": None, "type": None, "venue": None, "doi": None, "arxiv": None},
                "reason": judgment.get("reason", ""),
            })
            break
    return structured, semantic


def run_verification(bib_path):
    check_llm_status()
    with open(bib_path, "r", encoding="utf-8") as handle:
        database = bibtexparser.load(handle)

    verified, double_check, hallucination = [], [], []
    session = requests.Session()
    session.headers["User-Agent"] = "BibTeX-Hallucination-Check/strict-field-audit"
    print(f"Strict audit started: {len(database.entries)} entries")

    with DDGS() as ddgs:
        for index, entry in enumerate(database.entries, 1):
            print(f"[{index}/{len(database.entries)}] {entry.get('ID')}: {clean_latex(entry.get('title', ''))[:70]}")
            candidates, errors = [], []
            for name, search in (("Crossref", crossref_candidates), ("OpenAlex", openalex_candidates)):
                try:
                    candidates.extend(search(entry, session))
                except Exception as error:
                    errors.append(f"{name}: {type(error).__name__}")

            candidates = [evaluate(entry, candidate) for candidate in candidates]
            passed, identities, problems = aggregate(entry, candidates)

            if not passed:
                raw_candidates = [{key: value for key, value in candidate.items() if key not in {"checks", "title_similarity"}} for candidate in candidates]
                for candidate in doi_official_candidates(entry, raw_candidates, session):
                    candidates.append(evaluate(entry, candidate))
                passed, identities, problems = aggregate(entry, candidates)

            # arXiv is queried for every unresolved record and every arXiv entry.
            if not passed or venue_code(bib_venue(entry)) == "arxiv":
                try:
                    candidates.extend(evaluate(entry, candidate) for candidate in arxiv_candidates(entry, session))
                    time.sleep(3)
                except Exception as error:
                    errors.append(f"arXiv: {type(error).__name__}")
                passed, identities, problems = aggregate(entry, candidates)

            # Direct publisher/official URLs are additional identity evidence.
            try:
                direct = publisher_candidate(entry, session)
                if direct:
                    candidates.append(evaluate(entry, direct))
            except Exception as error:
                errors.append(f"Publisher URL: {type(error).__name__}")

            if not passed:
                try:
                    claimed = claimed_venue_candidate(entry, session)
                    if claimed:
                        candidates.append(evaluate(entry, claimed))
                except Exception as error:
                    errors.append(f"Claimed venue index: {type(error).__name__}")

            candidates.sort(key=lambda c: (same_work(c), c["checks"].get("authors") is True, c["title_similarity"]), reverse=True)
            passed, identities, problems = aggregate(entry, candidates)
            artifact = official_artifact_candidate(entry, candidates)
            if artifact:
                verified.append(make_record(
                    entry,
                    "Verified",
                    f"Exact official artifact found at {urllib.parse.urlparse(artifact['url']).netloc.lower()}.",
                    [artifact],
                ))
                print("   -> Verified (official artifact)")
                continue
            if passed:
                sources = ", ".join(dict.fromkeys(c["source"] for c in identities))
                warnings = cross_source_warnings(candidates)
                conflict_evidence = [
                    candidate for candidate in candidates
                    if candidate["checks"].get("title") is True and candidate not in identities
                ]
                verified.append(make_record(
                    entry,
                    "Verified",
                    f"All required fields match across {sources}.",
                    identities + conflict_evidence,
                    warnings,
                ))
                print("   -> Verified")
                continue

            web_structured, web_semantic = [], []
            try:
                web_structured, web_semantic = official_web_candidates(entry, ddgs, session)
            except Exception as error:
                errors.append(f"Official web: {type(error).__name__}")

            if web_structured:
                discovered_ids = {
                    candidate.get("arxiv_id") for candidate in web_structured
                    if candidate.get("arxiv_id")
                }
                known_ids = {candidate.get("arxiv_id") for candidate in candidates if candidate.get("arxiv_id")}
                for identifier in sorted(discovered_ids - known_ids):
                    try:
                        lookup_entry = dict(entry)
                        lookup_entry["eprint"] = identifier
                        candidates.extend(
                            evaluate(entry, candidate)
                            for candidate in arxiv_candidates(lookup_entry, session)
                        )
                        time.sleep(3)
                    except Exception as error:
                        errors.append(f"arXiv discovered-ID lookup: {type(error).__name__}")
                candidates.extend(evaluate(entry, candidate) for candidate in web_structured)
                candidates.sort(key=lambda c: (same_work(c), c["checks"].get("authors") is True, c["title_similarity"]), reverse=True)
                passed, identities, problems = aggregate(entry, candidates)
                artifact = official_artifact_candidate(entry, candidates)
                if artifact:
                    verified.append(make_record(
                        entry,
                        "Verified",
                        f"Exact official artifact found at {urllib.parse.urlparse(artifact['url']).netloc.lower()}.",
                        [artifact],
                    ))
                    print("   -> Verified (official artifact)")
                    continue
                if passed:
                    warnings = cross_source_warnings(candidates)
                    conflict_evidence = [
                        candidate for candidate in candidates
                        if candidate["checks"].get("title") is True and candidate not in identities
                    ]
                    verified.append(make_record(
                        entry,
                        "Verified",
                        "All required fields match, including an official publication page.",
                        identities + conflict_evidence,
                        warnings,
                    ))
                    print("   -> Verified (official page)")
                    continue

            if identities:
                exact_candidates = [candidate for candidate in candidates if candidate["checks"].get("title") is True]
                focused_problems = confirmed_problem_names(problems, candidates) or problems
                double_check.append(make_record(
                    entry,
                    "Double Check",
                    "Unconfirmed or inconsistent fields: " + ", ".join(focused_problems),
                    exact_candidates or identities,
                    cross_source_warnings(candidates),
                ))
                print("   -> Double Check:", ", ".join(focused_problems))
                continue

            if web_semantic:
                web = web_semantic[0]
                # Exact official corporate artifacts (e.g. a system card) have no
                # Crossref/arXiv record but are still verifiable primary sources.
                host = urllib.parse.urlparse(web["url"]).netloc.lower()
                corporate = normalize_text(entry.get("author", ""))
                bib_title = normalize_text(entry.get("title", ""))
                web_title = normalize_text(web.get("title", ""))
                title_is_contained = bib_title and (bib_title in web_title or web_title in bib_title)
                is_official_artifact = (
                    entry.get("ENTRYTYPE") == "misc"
                    and (web["checks"]["title"] or title_is_contained)
                    and corporate in normalize_text(host)
                )
                if is_official_artifact:
                    verified.append(make_record(entry, "Verified", f"Exact official artifact found at {host}.", [web]))
                    print("   -> Verified (official artifact)")
                else:
                    double_check.append(make_record(entry, "Double Check", web.get("reason") or "Official identity found but structured fields are incomplete.", [web]))
                    print("   -> Double Check (official-web match)")
                continue

            # An exact-title record with conflicting authors or publication
            # metadata is a real-work metadata problem, not a hallucination.
            title_candidates = [candidate for candidate in candidates if candidate["checks"].get("title") is True]
            if title_candidates:
                failed_fields = []
                for field in ("authors", "year", "type", "venue", "doi", "arxiv"):
                    values = [candidate["checks"].get(field) for candidate in title_candidates]
                    if False in values and True not in values:
                        failed_fields.append(field)
                reason = "Exact title found, but fields conflict or remain unconfirmed"
                if failed_fields:
                    reason += ": " + ", ".join(failed_fields)
                double_check.append(make_record(entry, "Double Check", reason, title_candidates))
                print("   -> Double Check (exact title with metadata conflict)")
                continue

            matching_identifier = [
                candidate for candidate in candidates
                if (
                    extract_arxiv_id(entry)
                    and candidate.get("arxiv_id") == extract_arxiv_id(entry)
                ) or (
                    entry.get("doi")
                    and candidate.get("doi")
                    and normalize_doi(entry.get("doi")) == normalize_doi(candidate.get("doi"))
                )
            ]
            if matching_identifier:
                double_check.append(make_record(
                    entry,
                    "Double Check",
                    "The persistent identifier resolves to a real work, but its current title, authors, year, or publication status differs from the BibTeX entry.",
                    matching_identifier,
                ))
                print("   -> Double Check (identifier match with metadata conflict)")
                continue

            # Strong near-title evidence is retained for human review rather
            # than mislabeled as a hallucination.  This covers visible title
            # suffixes and possible preprint-to-proceedings title changes, but
            # never promotes them to Verified without an exact identity match.
            plausible = [
                candidate for candidate in candidates
                if candidate["title_similarity"] >= 0.90
                or (
                    candidate["title_similarity"] >= 0.80
                    and candidate["checks"].get("authors") is True
                    and candidate["checks"].get("year") is not False
                )
            ]
            if plausible:
                plausible.sort(key=lambda c: c["title_similarity"], reverse=True)
                double_check.append(make_record(
                    entry,
                    "Double Check",
                    "A plausible title variant was found, but exact identity or metadata remains unconfirmed.",
                    plausible,
                ))
                print("   -> Double Check (plausible title variant)")
                continue

            error_note = (" Retrieval errors: " + ", ".join(errors)) if errors else ""
            hallucination.append(make_record(entry, "Hallucination", "No matching work found across Crossref, OpenAlex, arXiv, or official academic websites." + error_note, candidates))
            print("   -> Hallucination")

    generate_html_report(verified, double_check, hallucination)


# ============================================================================
# 6. Reports
# ============================================================================
def generate_html_report(verified, double_check, hallucination):
    def render(items, css_class):
        blocks = []
        for item in items:
            links = "".join(
                f"<li><b>{html.escape(source.get('source', ''))}</b>: "
                f"<a href='{html.escape(source.get('url', ''))}'>{html.escape(source.get('title') or source.get('url') or '')}</a> "
                f"[{html.escape(str(source.get('year') or ''))}; {html.escape(source.get('venue') or '')}]</li>"
                for source in item.get("sources", [])
            )
            blocks.append(
                f"<div class='item {css_class}'><h3>{html.escape(item['key'])}: {html.escape(item['title'])}</h3>"
                f"<div>{html.escape(item['reason'])}</div>"
                f"<div>{html.escape('; '.join(item.get('warnings', [])))}</div>"
                f"<ul>{links}</ul><pre>{html.escape(item['bib'])}</pre></div>"
            )
        return "".join(blocks)

    document = f"""<html><head><meta charset='utf-8'><style>
      body{{font-family:Arial,sans-serif;margin:40px;background:#f8f9fa;color:#222}}
      .item{{background:#fff;padding:18px;margin:18px 0;border-left:7px solid #198754}}
      .orange{{border-left-color:#ffc107}}.red{{border-left-color:#dc3545}}
      pre{{background:#212529;color:#f8f9fa;padding:12px;overflow-x:auto}}a{{color:#0056b3}}
    </style></head><body><h1>Strict BibTeX Audit</h1>
    <p>Verified: {len(verified)} | Double Check: {len(double_check)} | Hallucination: {len(hallucination)}</p>
    <h2>Verified ({len(verified)})</h2>{render(verified, '')}
    <h2>Double Check ({len(double_check)})</h2>{render(double_check, 'orange')}
    <h2>Hallucination ({len(hallucination)})</h2>{render(hallucination, 'red')}</body></html>"""
    with open("citation_audit_report.html", "w", encoding="utf-8") as handle:
        handle.write(document)
    with open("strict_results.json", "w", encoding="utf-8") as handle:
        json.dump({"verified": verified, "double_check": double_check, "hallucination": hallucination}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    generate_markdown_report(verified, double_check, hallucination)
    print("Generated citation_audit_report.html, citation_audit_report.md, and strict_results.json")


def generate_markdown_report(verified, double_check, hallucination):
    groups = (
        ("Verified", verified),
        ("Double Check", double_check),
        ("Hallucination", hallucination),
    )
    lines = [
        "# Strict BibTeX Audit",
        "",
        f"- Verified: **{len(verified)}**",
        f"- Double Check: **{len(double_check)}**",
        f"- Hallucination: **{len(hallucination)}**",
        "",
        "The checker compares complete normalized titles, ordered author lists, year, BibTeX/work type, final venue or arXiv-only status, and DOI/arXiv/URL identity across multiple retrieved candidates. `and others` is accepted only after an exact ordered author prefix.",
        "",
    ]
    for heading, records in groups:
        lines.extend([f"## {heading} ({len(records)})", ""])
        for record in records:
            fields = record.get("bib_fields", {})
            lines.extend([
                f"### `{record['key']}` — {clean_latex(record['title'])}",
                "",
                f"**Decision:** {record['reason']}",
                "",
                f"**BibTeX metadata:** {fields.get('entry_type') or 'N/A'}; {fields.get('year') or 'N/A'}; {clean_latex(fields.get('venue') or 'N/A')}",
                "",
            ])
            if record.get("sources"):
                lines.append("**Evidence:**")
                lines.append("")
            if record.get("warnings"):
                lines.append("**Cross-source notes:** " + "; ".join(record["warnings"]))
                lines.append("")
                for source in record["sources"]:
                    checks = source.get("checks", {})
                    check_text = ", ".join(
                        f"{field}={value}"
                        for field, value in checks.items()
                        if value is not None
                    )
                    label = clean_latex(source.get("title") or source.get("url") or "untitled result")
                    url = source.get("url") or ""
                    link = f"[{label}]({url})" if url else label
                    lines.append(
                        f"- {source.get('source', 'Source')}: {link}; "
                        f"year={source.get('year') or 'N/A'}; venue={clean_latex(source.get('venue') or 'N/A')}; "
                        f"checks: {check_text or 'structured fields unavailable'}"
                    )
                lines.append("")
    Path("citation_audit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run_verification(sys.argv[1] if len(sys.argv) > 1 else "main.bib")
