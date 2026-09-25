# BibTeX Citation Audit Tool（文献自动核查与防幻觉工具）

[English](README.md) | 中文

这是一个只读、基于可核验证据的 BibTeX 审查工具，用于发现虚构文献和书目信息错误。工具会从 Crossref、OpenAlex、arXiv、出版商页面及官方学术网站检索多个候选记录，要求相关字段能够由同一条一致的来源记录支持，并仅在确定性规则无法消除歧义时使用 Codex 进行最终语义判断。

## 核验内容

工具逐项检查每条文献的：

- 完整规范化标题，包括预印本与正式发表版本的标题变化；
- 完整作者名单和作者顺序；
- `and others` 之前是否为准确、有序的作者前缀；
- 年份和论文类型；
- 最终会议或期刊，以及是否仍仅为 arXiv 预印本；
- DOI、arXiv ID 和 URL 是否指向同一篇论文；
- 官方页面、出版商 metadata、Crossref、OpenAlex 与 arXiv 是否相互一致。

输入的 `.bib` 文件始终以只读方式打开，工具不会改写原文件。

## 检索与判定流程

1. **结构化检索：** 分别比较最多五条 Crossref 和 OpenAlex 候选，不再直接采用第一条搜索结果。
2. **arXiv 核验：** 直接解析已有 arXiv ID，同时进行多候选标题检索；当前 arXiv 记录会与旧版本和 BibTeX 信息共同核对。
3. **第一方来源核验：** 解析条目中的 DOI 或 URL、出版商 metadata、官方 proceedings 索引和学术白名单网站。
4. **严格字段比较：** 规范化 LaTeX 与 Unicode，比较完整标题和有序作者列表，并要求相关字段由同一条一致候选记录支持，禁止从互不兼容的候选中拼接字段后放行。
5. **Codex 兜底判断：** 当确定性证据仍有歧义时，Codex 只比较已检索到的证据与 BibTeX，并以 JSON Schema 约束输出。Codex 不负责凭空搜索或生成文献。

旧实现中的宽松规则已被移除：标题前若干字符相同不能直接通过，Crossref 第一条结果不能直接采用，查询时使用第一作者也不能代替完整作者校验。

## 三类结果

- **Verified：** 一条一致的权威记录支持所有必要书目信息。
- **Double Check：** 找到了真实或高度匹配的论文，但部分字段冲突、不完整或对应不同版本。这**不等于文献错误**。例如，正确的 arXiv 引用可能仅因为后来出现正式会议版本而进入该类。
- **Hallucination：** 在结构化数据库和官方学术网站中均未找到可信匹配。这是高风险提示；当检索服务异常时，仍不能替代最终人工判断。

来源冲突时，建议依次以最终论文 PDF 和作者更正记录、会议或期刊官方页面、出版商/DOI metadata、当前 arXiv 页面、书目索引为准；Google Scholar 等聚合平台仅作为辅助线索。

## 环境要求

安装 Python 依赖：

```bash
pip install bibtexparser requests ddgs
```

安装并登录 Codex CLI，确保 `codex` 位于 `PATH`。如果使用非标准安装位置，可设置：

```bash
export CODEX_BIN=/path/to/codex
```

建议为 Crossref polite pool 配置邮箱：

```bash
export CROSSREF_EMAIL=you@example.com
```

如果 Codex 不可用，确定性检索和字段比较仍会继续，但有歧义的条目不会被提升为 `Verified`。

## 使用方法

直接运行：

```bash
python check_citation_v0_dev.py references.bib
```

如果不提供路径，脚本默认读取当前目录中的 `main.bib`。

如需通过运行前后哈希证明输入文件未改变，可使用：

```bash
./run_upstream_codex_audit.sh references.bib audit_results
```

可通过 `PYTHON=/path/to/python` 指定解释器。

## 输出报告

每次运行生成：

- `citation_audit_report.html`：可交互证据报告；
- `citation_audit_report.md`：便于人工审阅的 Markdown 报告；
- `strict_results.json`：可供后续程序处理的完整结构化结果。

每条记录均包含原始 BibTeX、判定原因、检索来源、URL、规范化 metadata、逐字段结果及跨来源差异提示。

## 测试

```bash
python -m unittest -v test_strict_matching.py
```

回归测试覆盖 LaTeX 规范化、复合姓氏、姓名缩写、完整作者顺序、`and others`、venue 别名、arXiv 到正式发表版本变化、来源冲突，以及“所有字段必须由同一条一致记录支持”等关键行为。

## 局限性

- 第三方数据库本身可能存在错误，最终应以论文和出版方的正式记录为准。
- 预印本与正式发表版本可能存在合理差异，因此 `Double Check` 通常意味着需要选择引用版本，而不是自动替换。
- 网络故障或限流可能降低检索覆盖率。
- 投稿前仍应由作者对所有结果进行最终确认。
