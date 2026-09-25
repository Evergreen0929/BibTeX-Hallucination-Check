# BibTeX Citation Audit & Anti-Hallucination Tool

English | [中文](README_zh.md)

A read-only, evidence-backed BibTeX auditor for detecting fabricated citations and metadata errors. It retrieves multiple candidate records from Crossref, OpenAlex, arXiv, publisher pages, and official academic websites, verifies bibliographic fields against a coherent source record, and uses Codex only as a final semantic judge for ambiguous matches.

## What Is Verified

Each entry is checked field by field:

- complete normalized title, including preprint-to-publication title changes;
- complete author list and order;
- whether `and others` follows an exact ordered author prefix;
- publication year and work type;
- final conference or journal venue versus arXiv-only status;
- DOI, arXiv ID, and URL identity;
- consistency among official pages, publisher metadata, Crossref, OpenAlex, and arXiv.

The input `.bib` file is opened read-only and is never rewritten.

## Retrieval and Decision Pipeline

1. **Structured retrieval:** Query up to five Crossref and OpenAlex candidates instead of trusting the first search result.
2. **arXiv verification:** Resolve a claimed arXiv ID directly and also search multiple title candidates. The current arXiv record is compared with older versions and with the BibTeX entry.
3. **Primary-source verification:** Parse a claimed DOI or URL, publisher metadata, official proceedings indexes, and whitelisted academic domains.
4. **Strict field comparison:** Normalize LaTeX and Unicode, compare full titles and ordered authors, and require one coherent candidate to support the relevant fields. Fields cannot be assembled from incompatible records.
5. **Codex fallback:** When deterministic evidence remains ambiguous, Codex compares only the retrieved evidence with the BibTeX entry and returns schema-constrained JSON. It does not invent a citation or serve as the primary search engine.

The previous shortcuts are intentionally removed: a shared title prefix is not sufficient, the first Crossref hit is not accepted automatically, and querying by the first author does not replace full author verification.

## Result Classes

- **Verified:** A coherent authoritative record supports all required metadata.
- **Double Check:** A real or strongly matching work was found, but one or more fields conflict, remain incomplete, or describe a different version. This does **not** automatically mean the citation is wrong. For example, a valid arXiv citation may be flagged because a final proceedings version now exists.
- **Hallucination:** No credible matching work was recovered after structured and official-web searches. This is a high-risk signal, not a substitute for final human review when retrieval services were unavailable.

When sources disagree, prefer the final paper PDF and correction record, then the official proceedings or publisher page, DOI metadata, current arXiv metadata, bibliographic indexes, and finally search aggregators such as Google Scholar.

## Requirements

Python dependencies:

```bash
pip install bibtexparser requests ddgs
```

Install and authenticate the Codex CLI, then ensure `codex` is on `PATH`. A nonstandard executable can be supplied through `CODEX_BIN`:

```bash
export CODEX_BIN=/path/to/codex
```

For Crossref's polite pool, optionally provide an email address:

```bash
export CROSSREF_EMAIL=you@example.com
```

If Codex is unavailable, deterministic retrieval and comparison still run, but ambiguous entries are not promoted to `Verified`.

## Usage

Run the checker directly:

```bash
python check_citation_v0_dev.py references.bib
```

If no path is provided, it reads `main.bib` from the current directory.

For a hash-guarded run that proves the input file was unchanged:

```bash
./run_upstream_codex_audit.sh references.bib audit_results
```

Use `PYTHON=/path/to/python` to select a specific interpreter.

## Reports

Every run creates:

- `citation_audit_report.html`: interactive evidence report;
- `citation_audit_report.md`: review-friendly Markdown report;
- `strict_results.json`: complete structured results for downstream processing.

Each record includes the original BibTeX, decision reason, retrieved sources, URLs, normalized metadata, per-field checks, and cross-source warnings.

## Tests

```bash
python -m unittest -v test_strict_matching.py
```

The regression suite covers LaTeX normalization, compound surnames, initials, complete author order, `and others`, venue aliases, arXiv-to-final publication changes, conflicting sources, and the requirement that all fields be supported by one coherent record.

## Limitations

- Third-party metadata can contain errors; the official paper and publisher record remain authoritative.
- Preprints may legitimately differ from their final versions. `Double Check` records therefore require a version choice, not automatic replacement.
- Network failures and rate limits can reduce retrieval coverage.
- No automated result should replace final author review before submission.
