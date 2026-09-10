# Sci-Hub PDF 下载

显式使用 Sci-Hub 来源：

```powershell
python -m pip install -e '.[pdf]'
paper-fetch scihub --query '10.1038/nature12373' --output-dir ./papers
paper-fetch scihub --query 'Nanometre-scale thermometry in a living cell' --output-dir ./papers
paper-fetch scihub --query-file ./references.txt --output-dir ./papers
```

`references.txt` 使用 UTF-8，每行一篇 DOI、论文标题或含 DOI 的参考文献，空行忽略。
含 DOI 的输入直接使用该 DOI；标题或没有 DOI 的引文复用仓库的 Crossref 解析器。
解析结果不唯一时返回 `ambiguous_query`，需要补充 DOI 或准确标题，不自动猜选论文。

默认入口为 `https://sci-hub.ru`。可以用 `--base-url URL` 覆盖；重复传入多个
入口时按顺序尝试。`--timeout 30` 设置每个网络请求的超时秒数。

```powershell
paper-fetch scihub --query-file ./references.txt --base-url https://sci-hub.ru --output-dir ./papers
```

流程是：识别论文 DOI → 请求 Sci-Hub 页面 → 必要时完成该页面的 ALTCHA v1
SHA-256 计算验证 → 提取 iframe/embed/object/下载按钮中的 PDF 地址 → 下载原文件 →
调用现有 PDF 验收与原子保存接口。计算验证限制在 500 万次、20 秒以内；不执行网页
JavaScript。当前接入基于实际观察到的 `/captcha/challenge/ID` 和
`/captcha/solution/ID` 协议，未知验证方式会明确失败。

PDF 必须可解析且具有与请求匹配的独立 DOI 或标题证据。扫描件或旧论文若没有
可提取的身份信息，会返回 `identity_unverified`，不会标记为下载成功。
`--overwrite` 允许替换同名不同内容；相同字节保持幂等。

输出目录的 `scihub-results.jsonl` 会逐篇追加结果。每条记录包含状态、来源、DOI、
文件路径、字节数、页数、SHA-256 和身份验收结果；失败包含错误代码和说明。
部分篇目失败时继续处理后续条目，最终退出码为 1；全部成功为 0。
这份文件是 Sci-Hub 命令自己的下载日志，未伪装成普通 `fetch` 的 manifest。

这个命令使用只在内存中存活的独立 Cookie 会话，不读取出版社或学校的浏览器
profile、账号文件或登录态。TLS 校验保持开启；沿用项目的外部 URL 安全检查，
拒绝本地地址、不安全跳转及超出大小限制的响应。保存的来源 URL 去除查询参数。
普通 `paper-fetch fetch --download-pdf` 的来源行为不变。

## 验证证据

2026-09-11 在 Windows 上分别真实运行上述 DOI 和标题示例：两者均通过当前 Sci-Hub 验证并保存
6 页、943,776 字节的 PDF，DOI 匹配，SHA-256 为：

```text
5929b8e4f239dccd09791d9467943830b14581fa412ff076e019a955a639bf10
```

这证明该次请求的链路可用，不代表所有论文均被收录或所有网络均可访问。
同次测试中 `10.1038/171737a0` 因无法取得独立身份信号而被拒绝保存。

实现前检查了 [SciDownl](https://github.com/Tishacy/SciDownl/tree/3e66abd7bd5c0c003d98780063a9212f44335920)
和 [scihub.py](https://github.com/zaytoun/scihub.py/tree/82532fc4fe9b405e1286f60676b776696f4bc844)
的解析方式。当前适配器使用仓库已有解析和校验能力，未引入这两个项目的依赖。
计算验证参考 [ALTCHA v1 协议](https://github.com/altcha-org/altcha-lib)。
