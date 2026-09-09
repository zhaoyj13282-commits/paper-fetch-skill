---
name: paper-fetch-skill
description: "获取并核验指定论文全文，或探测全文可用性。用户要求获取、下载、归档论文，或阅读、总结、比较、翻译指定论文且需要获取或核验全文时使用。支持 DOI、URL、arXiv ID、标题、引用条目及搜索产生的明确候选；代理经搜索发现明确论文候选，并需要获取或核验其全文作为回答依据时，也使用本技能。不承担尚无明确候选的开放式领域检索。"
---

# 论文抓取技能

先确认论文身份，再按意图获取全文或探测可用性。抓取后验收实际文本或文件；探测只报告可用性证据。搜索工具只负责发现候选，不得用搜索摘要或网页片段冒充论文全文。

## 核心契约

- 预设只补全未指定项；沿用用户明确的路径、资产范围、执行面和已有覆盖授权，不重复确认。身份、访问权限和数据完整性约束仍须满足。
- 只允许状态机的 BLOCKING 白名单暂停工作。普通上下文阅读/总结不因保存策略缺失而阻塞，后端选择本身也不要求用户确认。
- 三个批量工具 `batch_resolve`、`batch_check`、`batch_fetch` 每次最多 50 条；输入规范化时即分块并保留原始 index，解析后跨块 DOI 去重。阶段依赖有序，同一阶段内身份独立的论文允许受控并发。
- 按当前预设复用合格本地全文或同 scope 精确缓存；临时阅读不为查询缓存而要求目录。多篇完整阅读沿用单篇阅读预设取得每篇正文，不先批量抓取再重复获取；compact 无正文，bounded 可能截断。
- 只在 provider、凭证或浏览器运行时可能影响结果时检查状态；browser provider 首次联网抓取前先做静态检查，按需 live 预检。普通工具在实际启动浏览器前自动准备 managed runtime，尊重已有渠道和固定版本；仅在结果明确要求时进入人工 auth，不绕过合法访问边界。
- 真实抓取和文件复用使用统一 acceptance 并核验实际响应或文件；不得用 `.gitignore` 或 `git status` 是否变化代替文件验收。仅探测时核对逐项 `probe_state`、证据、错误和未调度状态，不要求全文 acceptance，也不自动升级为抓取。
- 不要仅因为本地没有 PDF 或缓存文本文件就断定论文不可读；也不要把 abstract-only 或 metadata-only 报告成全文成功。
- Browser HTML 失败但 PDF/ePDF fallback 成功时，仍按 trace 中的精确 browser code 报告降级，并要求 `acceptance.overall=degraded`；不得用顶层 `status=ok` 抹掉 HTML failure provenance。
- 参考文献列表或 web search 已产生候选论文后，先进入身份状态机；没有可核验候选的开放式发现任务不由本技能替代。

## 按需参考

- 开始任务先读唯一状态机和 BLOCKING 白名单：[`references/workflow.md`](references/workflow.md)。
- 输入超过 50 条时先读分块规则；身份明确后读共同规则、当前五个预设之一及本地/cache 分支，确定参数和必要的 scope 后再检查本地/cache；选定执行面后核对落盘矩阵：[`references/presets.md`](references/presets.md)。
- 抓取或复用时读七个分面、响应验收和最终报告；请求资产、复用文件、归档或批量任务再读对应章节。仅探测时读探测核对与报告：[`references/acceptance.md`](references/acceptance.md)。
- 标题解析读 MCP Tools；需精确参数、返回值、cache 或 provider catalog 时读对应章节：[`references/tool-contract.md`](references/tool-contract.md)。
- 凭证或工具链影响任务时读相关配置及诊断章节；runtime 缺失时读准备与授权：[`references/environment.md`](references/environment.md)。
- 使用 CLI 时读单篇或批量流程；入口不可用时再读窄 fallback：[`references/cli-workflow.md`](references/cli-workflow.md)。
- 出现失败、限流、需解释的降级或准备重试时，读总尝试次数及对应决策表行：[`references/failure-handling.md`](references/failure-handling.md)。

验收后宿主继续完成用户要求的总结、比较或翻译等任务；获取报告不替代原任务。检查实际文本及截断标记，不把摘要、元数据或片段说成已读全文。
