# BibTeX Citation Audit & Anti-Hallucination Tool

A semi-automated BibTeX citation verification tool designed for academic researchers. By combining "multi-tier precise retrieval" with "Local Large Language Model (LLM) semantic evaluation," this tool automatically filters out dead links, typos, and AI-generated "hallucinated" citations in your `.bib` files, generating a highly intuitive HTML audit report.

## ⚙️ How It Works (The Funnel Mechanism)

This tool employs a three-tier funnel filtering system to rigorously verify the authenticity of each citation:

1. **Tier 1: Crossref Direct Verification (Gold Standard)**
   The tool first queries the official Crossref API with the BibTeX title. It uses the Crossref "Polite Pool" (via email registration) for faster and more stable responses. If an exact match is found, the citation is marked as `Verified` and bypasses all subsequent computation.
2. **Tier 2: arXiv Deep Search + LLM Semantic Audit**
   If not found, the tool extracts core keywords from the title and queries the arXiv API for up to 10 candidates. Each candidate's title, author, and summary are sent to the locally deployed LLM alongside the original BibTeX. If the LLM determines a semantic match, the search loop breaks immediately, and the paper is flagged for `Double Check`.
3. **Tier 3: Web Fallback (Strict Whitelist) + LLM Semantic Audit**
   If the paper isn't on arXiv, the tool uses DuckDuckGo to scrape web search results. **Crucially, it strictly filters results using an academic whitelist** (e.g., `ieee.org`, `thecvf.com`, `neurips.cc`). Non-official sources like GitHub or personal blogs are automatically discarded. Valid snippets are then evaluated by the LLM.
4. **Missing (Dead End / Hallucination Risk)**
   If a citation survives all deep searching across databases without a match, it is flagged as a high-risk typo or "AI hallucination" and pushed to the red `Missing` alert zone.

### 🛡️ Core Feature: How It Maximizes the Prevention of LLM Hallucinations

Many academic tools hallucinate (inventing non-existent paper links or volume numbers) when using LLMs for citations. This tool fundamentally eliminates this issue through its architecture:

* **Role Reversal (LLM as a Judge, Not a Search Engine):** The tool NEVER asks the LLM "Does this paper exist?" or "Give me the URL for this paper." The LLM acts strictly as a "referee." Its only job is to compare Entity A (the BibTeX info) with Entity B (the raw text fetched by the crawler) and determine if they refer to the same paper.
* **Information Isolation (Context-Only Prompting):** All URLs, summaries, and metadata come from authentic external APIs. The LLM is forced via strict prompting (with `temperature=0.0`) to output only a structured JSON (`is_match` and `reason`). This cuts off the model's ability to "hallucinate" facts from its pre-trained weights.
* **Physical Evidence Binding:** Every candidate URL presented in the report is a valid, clickable link genuinely retrieved by the search engine from an official academic domain.

## 🛠️ Environment Configuration

The script requires standard Python retrieval libraries and a locally running LLM API service.

### 1. Python Dependencies

Install the required packages using pip:

```bash
pip install bibtexparser requests duckduckgo-search
```
* `bibtexparser`: For parsing and reading `.bib` files.
* `requests`: For handling API calls to Crossref, arXiv, and the local vLLM endpoint.
* `duckduckgo-search` (`ddgs`): For performing free, unrestricted web fallback searches.

### 2. Local LLM Setup (Recommended: vLLM)

This tool relies on an OpenAI API-compatible local LLM server for semantic matching. The default configuration points to `Qwen/Qwen3-235B-A22B`.

If you have sufficient GPU resources and use `vLLM`, launch the server in a separate terminal (adjust `--tensor-parallel-size` according to your hardware):

```bash
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen3-235B-A22B \
    --tensor-parallel-size 8 \
    --port 8001
```

*Note: If you are using a different inference framework or model weights, update the `LOCAL_LLM_URL` and `MODEL_NAME` variables at the top of the script. If the script fails to detect an active LLM, it will gracefully degrade into an "LLM-Disabled" manual review mode.*

## 🚀 Usage

1. Ensure your local LLM service (port 8001) is running and accessible.
2. Run the script from the terminal, passing your BibTeX file as an argument:

```bash
python check_citation.py my_paper.bib
```
*(If no argument is provided, the script will default to looking for `main.bib` in the current directory).*

*🔥 **Robustness Tip:** If you press `Ctrl+C` at any point during a long execution, the script will catch the interrupt signal and **instantly save your current progress into the HTML report**. No search data will be lost.*

## 📄 Output Report

Upon completion, a `citation_audit_report.html` file will be generated in your directory. Open it in a web browser to view the results categorized into three sections:

* ✅ **Verified (No action needed)**: Authentic papers absolutely confirmed by the Crossref API.
* ⚠️ **Double Check (Manual review advised)**: Papers found via deep search on arXiv or official Web domains, which the LLM determined to be a semantic match. The report provides the source URL, the LLM's reasoning, and the original raw BibTeX code for quick copy-pasting and correction.
* ❌ **Missing (High hallucination risk)**: Papers that could not be found anywhere. Pay special attention to these, as they are likely severe typos or generated by AI.

## ⚠️ Disclaimer

**This tool is a semi-automated assistant and is NOT a substitute for rigorous academic review.**
1. **Dependency Limits:** The automated confirmation (`Verified` status) relies entirely on the metadata accuracy and network availability of third-party databases (Crossref/arXiv).
2. **Human Verification Required:** All matches determined by the LLM (`Double Check`) represent only semantic similarity. **You must personally click the provided URLs to verify the specific volume, year, and venue of the publication.**
3. **Search Fluctuations:** The web search component (DDGS) may occasionally be subject to IP rate-limiting or network instability.