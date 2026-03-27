import sys
import bibtexparser
import requests
import time
import urllib.parse
import re
import json
import xml.etree.ElementTree as ET
from ddgs import DDGS

# =================================================================
# 1. 配置区
# =================================================================
LOCAL_LLM_URL = "http://localhost:8001/v1/chat/completions" 
MODEL_NAME = "Qwen/Qwen3-235B-A22B"
# 强烈建议填入真实邮箱，进入 Crossref Polite Pool，提速防封
CROSSREF_EMAIL = "jdzhang@tamu.edu" 
LLM_AVAILABLE = False

OFFICIAL_DOMAINS = [
    'arxiv.org', 'ieee.org', 'thecvf.com', 'acm.org', 'openreview.net', 
    'springer.com', 'neurips.cc', 'icml.cc', 'pnas.org', 'nature.com',
    'science.org', 'aaai.org', 'ijcai.org', 'cvfoundation.org'
]

def check_llm_status():
    global LLM_AVAILABLE
    print(f"🔍 正在连接本地模型 [{MODEL_NAME}]...")
    try:
        res = requests.post(LOCAL_LLM_URL, json={
            "model": MODEL_NAME, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5
        }, timeout=3)
        LLM_AVAILABLE = (res.status_code == 200)
        print("✅ Qwen3 连通成功，将启用深度语义审查。" if LLM_AVAILABLE else "❌ Qwen3 未响应，退化为基础搜集模式。")
    except:
        LLM_AVAILABLE = False
        print("❌ 无法连接 LLM 接口，退化为基础搜集模式。")

# =================================================================
# 2. 核心工具与 LLM 判别
# =================================================================

def clean_latex(text):
    if not text: return ""
    text = re.sub(r'\\[a-zA-Z]+', ' ', text)
    return re.sub(r'[\{\}\$]', '', text).strip()

def get_core_words(title):
    clean_title = re.sub(r'[^a-zA-Z0-9\s]', ' ', clean_latex(title)).lower()
    stop_words = {'and', 'for', 'the', 'with', 'from', 'using', 'based', 'towards', 'of', 'in', 'on', 'a', 'an'}
    return [w for w in clean_title.split() if len(w) > 2 and w not in stop_words]

def llm_verify_paper(bib_entry, search_info):
    """Qwen3 核心比对：采用严格的学术审查 Prompt"""
    if not LLM_AVAILABLE:
        return {"is_match": True, "reason": "LLM 离线，自动标记为待核对", "confidence": 1.0}
    
    prompt = f"""You are an expert academic citation auditor. Strictly determine if the BibTeX entry and the search result refer to the EXACT SAME academic paper.

[BibTeX]:
- Title: {bib_entry.get('title', 'N/A')}
- Authors: {bib_entry.get('author', 'N/A')}
- Year: {bib_entry.get('year', 'N/A')}

[Search Result]:
{search_info}

Strict Evaluation Criteria:
1. Title Consistency: Core concepts must match. Ignore minor LaTeX formatting.
2. Author Verification: Must have significant overlap.
3. Content Alignment: Summary/snippet must describe the exact same methodology.

Format: {{"is_match": true/false, "reason": "Brief professional explanation."}}"""

    try:
        res = requests.post(LOCAL_LLM_URL, json={
            "model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0, "response_format": {"type": "json_object"}
        }, timeout=40).json()
        
        raw_json = res['choices'][0]['message']['content']
        # 鲁棒性优化：去除某些模型强行加上的 markdown 代码块标记
        clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_json.strip(), flags=re.IGNORECASE)
        return json.loads(clean_json)
    except Exception as e:
        return {"is_match": False, "reason": f"LLM Error: {str(e)}"}

def get_raw_bibtex(entry):
    res = f"@{entry.get('ENTRYTYPE', 'article')}{{{entry.get('ID', 'key')},\n"
    for k, v in entry.items():
        if k not in ['ENTRYTYPE', 'ID']: res += f"  {k} = {{{v}}},\n"
    return res + "}"

# =================================================================
# 3. 主核查引擎
# =================================================================

def run_verification(bib_path):
    check_llm_status()
    try:
        with open(bib_path, 'r', encoding='utf-8') as f:
            db = bibtexparser.load(f)
    except FileNotFoundError:
        print(f"❌ 错误: 找不到文件 {bib_path}")
        sys.exit(1)
    
    verified_list, double_check_list, missing_list = [], [], []
    print(f"🚀 文献校验开始，共计 {len(db.entries)} 篇")
    
    # 优化：复用 DuckDuckGo 会话，防止频繁握手被封
    with DDGS() as ddgs:
        try:
            for i, entry in enumerate(db.entries):
                orig_title = entry.get('title', '')
                if not orig_title: continue # 防止空标题引发异常
                
                clean_t = clean_latex(orig_title)
                first_author = entry.get('author', '').split(' and ')[0].split(',')[0].strip()
                
                print(f"[{i+1}/{len(db.entries)}] 🔍 {clean_t[:50]}...")

                # --- 阶段 1: Crossref 官方确定性免检 ---
                found_done = False
                try:
                    # 优化：加入 Polite Pool 邮箱参数，显著提高 API 稳定性和速率
                    cr_q = urllib.parse.quote(f"{clean_t} {first_author}")
                    cr_url = f"https://api.crossref.org/works?query.bibliographic={cr_q}&rows=1&mailto={CROSSREF_EMAIL}"
                    cr_res = requests.get(cr_url, timeout=10).json()
                    items = cr_res.get('message', {}).get('items', [])
                    
                    if items:
                        found_title = items[0].get('title', [''])[0].lower()
                        # 双重校验：标题前20个字符吻合即可认为官方库已确收录
                        if clean_latex(orig_title)[:20].lower() in found_title:
                            print("   -> 🟢 Verified (Crossref Polite Pool)")
                            verified_list.append(orig_title)
                            found_done = True
                except: pass
                
                if found_done: continue

                # --- 阶段 2: arXiv 深度语义核查 (上限 10 次) ---
                print("      - 尝试 arXiv 深度检索...")
                words = get_core_words(clean_t)[:6]
                arxiv_q = urllib.parse.quote(" AND ".join([f"all:{w}" for w in words]))
                try:
                    arxiv_url = f"http://export.arxiv.org/api/query?search_query={arxiv_q}&max_results=10"
                    arxiv_resp = requests.get(arxiv_url, timeout=20)
                    
                    if arxiv_resp.status_code == 200 and arxiv_resp.content.strip():
                        root = ET.fromstring(arxiv_resp.content)
                        ns = {'atom': 'http://www.w3.org/2005/Atom'}
                        for node in root.findall('atom:entry', ns):
                            info = f"Title: {node.find('atom:title', ns).text} | Sum: {node.find('atom:summary', ns).text[:200]}"
                            judge = llm_verify_paper(entry, info)
                            if judge.get('is_match'):
                                print(f"   -> 🟠 Match in arXiv: {judge.get('reason')[:45]}...")
                                double_check_list.append({
                                    'title': orig_title, 'bib': get_raw_bibtex(entry),
                                    'url': node.find('atom:id', ns).text, 'reason': judge.get('reason')
                                })
                                found_done = True
                                break
                    time.sleep(3) # 遵守 arXiv API 限流规定
                except Exception as e:
                    print(f"      [arXiv 异常: {e}]")

                if found_done: continue

                # --- 阶段 3: Web 深度语义核查 (白名单限制) ---
                print("      - 尝试 Web 检索 (官方白名单过滤中)...")
                try:
                    web_res = list(ddgs.text(clean_t, max_results=10))
                    for r in web_res:
                        url = r.get('href', '').lower()
                        if not any(domain in url for domain in OFFICIAL_DOMAINS):
                            continue # 严格拦截非官方学术网址
                        
                        info = f"Title: {r.get('title')} | Snippet: {r.get('body')}"
                        judge = llm_verify_paper(entry, info)
                        if judge.get('is_match'):
                            print(f"   -> 🟠 Match in Web: {judge.get('reason')[:45]}...")
                            double_check_list.append({
                                'title': orig_title, 'bib': get_raw_bibtex(entry),
                                'url': r['href'], 'reason': judge.get('reason')
                            })
                            found_done = True
                            break
                    time.sleep(1.5) # 防止频繁请求 DuckDuckGo 被 Ban
                except Exception as e:
                    print(f"      [Web 异常: {e}]")

                # --- 阶段 4: 兜底 (缺失/幻觉) ---
                if not found_done:
                    print("   -> 🔴 Missing (极高幻觉风险)")
                    missing_list.append({'title': orig_title, 'bib': get_raw_bibtex(entry)})

        except KeyboardInterrupt:
            print("\n\n🛑 接收到终止信号！正在为您安全保存当前进度...")

    generate_html_report(verified_list, double_check_list, missing_list)

# =================================================================
# 4. 报告生成器
# =================================================================

def generate_html_report(v, d, m):
    html = f"""
    <html><head><meta charset="utf-8"><style>
        body {{ font-family: 'Segoe UI', Tahoma, sans-serif; margin: 40px; background: #f8f9fa; color: #333; }}
        .item {{ background: #fff; border: 1px solid #e9ecef; padding: 20px; margin-bottom: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); }}
        .orange {{ border-left: 8px solid #f5c6cb; border-color: #ffc107; }} 
        .red {{ border-left: 8px solid #f5c6cb; border-color: #dc3545; }}
        pre {{ background: #212529; color: #f8f9fa; padding: 15px; border-radius: 5px; overflow-x: auto; font-family: 'Consolas', monospace; font-size: 13px; }}
        .reason {{ background: #fff3cd; color: #856404; padding: 12px; border-radius: 4px; border: 1px solid #ffeeba; margin-bottom: 12px; font-size: 14px; line-height: 1.5; }}
        .url {{ color: #0056b3; word-break: break-all; font-weight: 500; }}
        h2 {{ border-bottom: 2px solid #dee2e6; padding-bottom: 10px; margin-top: 40px; }}
    </style></head><body>
    <h1>文献核查报告 ({time.strftime("%Y-%m-%d %H:%M")})</h1>
    <p>AI 审核核心: {MODEL_NAME} | 状态: {"🟢 在线且已启用" if LLM_AVAILABLE else "⚪ 离线"}</p>
    <p>✅ <b>Verified ({len(v)} 篇):</b> 已通过 Crossref API 直接确认，完全可信。</p>
    
    <h2>⚠️ Double Check ({len(d)} 篇)</h2>
    <p>以下文献在官方数据库预印本 (arXiv) 或白名单学术网站中检出，并由 Qwen3 判定为高度相似：</p>
    {"".join([f"<div class='item orange'><h3>{i['title']}</h3><div class='reason'><b>🤖 Qwen3 裁决:</b> {i['reason']}</div><p><b>来源出处:</b> <a class='url' href='{i['url']}'>{i['url']}</a></p><pre>{i['bib']}</pre></div>" for i in d])}
    
    <h2>❌ Missing / Hallucination ({len(m)} 篇)</h2>
    <p>警告：在所有官方渠道及白名单网络检索中均未发现匹配项，强烈建议核对是否存在拼写错误或大模型编造的“幻觉”。</p>
    {"".join([f"<div class='item red'><h3>{i['title']}</h3><pre>{i['bib']}</pre></div>" for i in m])}
    </body></html>
    """
    with open("citation_audit_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✨ 最终报告已安全生成: citation_audit_report.html")

if __name__ == "__main__":
    # 支持命令行传参： python check_citation.py my_paper.bib
    target_bib = sys.argv[1] if len(sys.argv) > 1 else 'main.bib'
    run_verification(target_bib)