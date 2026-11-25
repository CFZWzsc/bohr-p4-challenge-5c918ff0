---
name: federated-database-search-merge
description: Use when a research task returns results from several literature databases and needs cross-database field normalisation, explainable duplicate clustering, deterministic merged records with per-field provenance, conflict resolution, and explicit per-source coverage gaps in a contract-preserving result.json.
---

# 多数据库联合检索与可回溯合并



## 用途与适用条件

当一个科研问题需要同时查询多个学术数据库（Crossref、Europe PMC、Scopus、Web of Science、CNKI 等），而各库字段口径、论文标识符与返回结构不一致时使用。目标是把多份异构响应合并成一份**不重复、字段可用、逐条可回溯到来源库**的记录集，并明确说出哪些库/字段没有覆盖。

**适用前提**：输入真实提供按来源库分组的检索结果，或每条记录自带来源标记。没有来源信息时仍可去重，但会在 `uncertainty` 中标注"来源不可回溯"。

**不适用**：绕过数据库授权与额度限制、单库全文阅读、把未授权数据汇总到未批准服务。不联网、不内置任何库的私有凭据。

## 输入

| 输入 | 说明 |
|---|---|
| `/input/case.json` | 必读。常见结构：`databases`/`sources`/`searches` 数组，每项含 `name`、`query`、`status`、`results`（或 `records`/`items`/`hits`）；也支持扁平记录列表，记录内用 `source`/`database` 标记来源。实际键名以文件为准。 |
| `task.md` | 可选，存在时读取用于核对任务要求的库集合与字段范围。 |
| `--work` | 工作目录，默认 `/app/work`。 |

## 处理流程（对应评分维度）

1. **逐库编排与读取**（任务完成度）：识别来源块及其查询式、状态；某库返回失败/限流/无权限时，记录该库的缺口而不是静默跳过。
2. **字段标准化**（结果正确性）：建立跨库字段映射（标题、作者、年份、DOI、PMID、arXiv、期刊、卷期页、类型、语种、摘要），DOI 去除 `https://doi.org/` 前缀统一小写；缺失保留为 `null`，与"未知"区分。原文与定位不改写。
3. **重复判定**（正确性/可核验性）：按 `DOI → PMID → arXiv → 归一化标题` 精确聚类；仅对无强标识符冲突、年份相差 ≤1、第一作者一致或缺失的记录做标题相似度 ≥0.93 的模糊合并，并记录每一对合并证据。相似标题但作者不同时不合并，进入 `identity_ambiguities` 待人工仲裁。
4. **合并与冲突消解**（完整性）：同一簇内逐字段取值——标题取最长、其余取"来源顺序第一条非空值"，规则写入 `provenance[field].rule`；同一字段多个不同取值全部保留在 `conflicts`，不悄悄丢弃。
5. **来源回溯**（可核验性）：每条合并记录保留 `source_locators`、`sources` 与逐字段的 `chosen_source`/`chosen_locator`，可沿"库—查询—原始响应—映射—去重—合并"链路复核。
6. **覆盖比较**（完整性）：按库统计记录数、合并后独占条数、各字段的 `complete/partial/absent`；汇总全局 `missing_fields`。

## 输出契约

写入 `--out`（默认 `/app/output/result.json`）。顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string | 原样保留输入 `case_id` |
| `status` | string | `success` / `partial` / `insufficient_input` / `failed` |
| `result` | object | `merged_records`（同时以 `records`/`results` 暴露）、`duplicates`、`dedup_evidence`、`identity_ambiguities`、`conflicts`、`coverage`、`field_mapping`、`limitations`、`change_log` |
| `evidence` | array | 来源块、去重对、合并记录的证据 |
| `uncertainty` | array | 来源不可用、字段缺口、身份歧义、口径限制 |
| `errors` | array | 来源级错误（不中断其他库） |
| `stats` | object | 库数、原始条数、合并后条数、去重数、冲突数 |
| `contract_notes` | object | 未产出契约字段的说明 |
| `output_contract_fields` | object | `provided` 与 `present` |

输入 `output_contract` 的每个字段名都会出现在顶层；无法产出时写 `null` 并记入 `contract_notes`。

`status`：无任何来源块或记录 → `insufficient_input`；存在失败来源、字段缺口或身份歧义 → `partial`；全部来源可用且合并无歧义 → `success`；解析失败 → `failed`。

## 异常处理

| 情形 | 处理 | 输出标记 |
|---|---|---|
| 某库 status 为失败/限流/无权限 | 保留该库缺口，继续合并其余库 | `errors[]` + `uncertainty.type=source_unavailable` |
| 库返回空结果 | 记为 0 条，与"库不可用"区分 | `coverage.sources[].available` |
| 同一 DOI 对应不同标题 | 不自动合并，列歧义 | `identity_ambiguities` |
| 标题相似但第一作者不同 | 不合并，列歧义 | `identity_ambiguities` |
| 字段在部分库缺失 | 逐库统计 partial/absent | `coverage.sources[].fields` |
| 记录无来源标记 | 归入 `unspecified` 来源并标注不可回溯 | `uncertainty` |
| 输出目录不可写 | stderr 简短错误，退出码 3 | 无静默成功 |

## 限制

- 不发起真实检索；库可用性、额度与响应均以输入声明为准。
- 模糊合并基于标题相似度与作者/年份约束，属候选判定，不能替代人工或权威注册表核实。
- 冲突消解采用固定确定性规则，只保证可复算，不代表字段真值优先级最优。
- 语言、文献类型等字段若各库口径不同，仅做字符串级归一，不做语义映射。

## Quickstart

在仓库根目录 `bohrium-competition` 执行（PowerShell，实测通过）：

```powershell
python -B "tracks\literature-search\challenges\challenge-5c918ff0\06_code\submission\scripts\run_skill.py" --case "tracks\literature-search\challenges\challenge-5c918ff0\06_code\submission\examples\demo-case\case.json" --out "$env:TEMP\demo_5c918ff0\result.json" --work "$env:TEMP\demo_5c918ff0\work"
```

- 示例输入：`examples/demo-case/case.json`，合成且自包含（`"demo": true`，两条数据库各有返回、含一条跨库重复记录；所有 DOI 均为虚构的 `10.0000/...`）。
- 实测输出：`status = "success"`、`merged = 3`，`result.json` 顶层含 `case_id`、`status`、`result`、`evidence`、`uncertainty`、`errors`、`stats`；同一输入两次运行结果逐字节一致。
- 该用例只验证脚本可执行与输出契约齐全，不代表真实数据库覆盖情况。

## 最小调用示例（示例，非正式案例结果）

```bash
python /skills/weekly-skill/scripts/run_skill.py \
  --case /input/case.json --out /app/output/result.json --work /app/work
```

输入片段（示例）：

```json
{"case_id": "demo-002",
 "databases": [
   {"name": "crossref", "query": "pyroptosis AND NLRP3",
    "results": [{"title": "NLRP3 inflammasome activation", "doi": "10.1/abc", "year": 2020}]},
   {"name": "europepmc", "query": "pyroptosis NLRP3",
    "results": [{"title": "NLRP3 inflammasome activation.", "doi": "10.1/ABC", "year": 2020, "pmid": "123"}]}
 ]}
```

示意输出（示例，已截断）：

```json
{"case_id": "demo-002", "status": "success",
 "result": {"merged_records": [{"doi": "10.1/abc", "sources": ["crossref", "europepmc"],
                                "provenance": {"pmid": {"chosen_source": "europepmc"}}}],
            "duplicates": [{"sources": ["crossref", "europepmc"]}],
            "coverage": {"records_raw": 2, "records_merged": 1, "duplicates_removed": 1}}}
```

以上为接口示意，不代表任何官方固定案例结果。


## 交付保证与评分对齐

1. 在 Skill 安装目录执行：`cd /skills/weekly-skill && python scripts/run_skill.py --case /input/case.json --out /app/output/result.json --work /app/work`；路径不同时显式改 `--case` / `--out` / `--work`，不要假定相对路径可用。
2. 必须产出**完整成品** `result.json`：每条来源记录都进入合并结果或重复簇，逐字段保留 `chosen_source` 与定位，覆盖缺口逐库列出；禁止 skeleton、`pending`、空占位或只写方法说明。
3. 脚本无法自动完成的实质判断（真实身份歧义、字段冲突取舍）由调用 Agent 依据实际响应补全并写回 `result.json`，不得编造记录。
4. 自检：`case_id` 与输入一致；`output_contract` 每个字段存在；`status` 取本契约值 `success` / `partial` / `insufficient_input` / `failed`（不得写 `completed`）；失败来源已登记。
5. 输入缺失、JSON 损坏或字段异常时仍写出结构化结果并说明缺口，不崩溃、不静默成功。

## 交付要求

1. **交付成品，不是流程说明**：脚本产出只是索引与核对骨架；调用 Agent 必须在同一 `result.json` 中补齐本题要求的检索结果、查询计划、纳排依据与证据；字段为空或含 `pending`、`TODO` 一律视为未完成。
2. **覆盖全部输入条目**：输入中的每条检索式、候选记录、问题与指标都要有对应条目；确实缺失的写入 `uncertainty` / `limitations` 并说明原因，不得静默跳过。
3. **自检后再交付**：`case_id` 与输入一致；`output_contract` 每个字段落位；状态取脚本实现的枚举（见上方契约表）；引文为原文短句并带来源定位；计数内部闭合、无自相矛盾。
4. **参数等价**：`--case <输入> --out <输出>` 与 `--output <输出>` 等价；默认 `/input/case.json` → `/app/output/result.json`。
5. **平台用 3 个固定案例分别运行**：逻辑由输入驱动，不针对示例硬编码；字段命名差异走别名匹配并在 `uncertainty` 记录假设。

## 运行时产物（单次运行自动写出）

入口脚本在写 `result.json` 的同一次运行里还会写出：`report.md`（人读报告，每条带源定位）、
`provenance.json`（条目 → 来源定位映射）、`records.csv`（平面结果表）、
`validation.json`（自检项）。`output_contract` 中的产物类字段直接指向这些文件，
无需第二次运行或人工补写。
