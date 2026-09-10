# 北大机构登录下载 ScienceDirect PDF

这个入口针对一篇已知的 ScienceDirect 文章，使用北大图书馆公布的
[WAYFless 机构登录路线](https://lib.pku.edu.cn/portal/news/0000002139)。

```powershell
python -m pip install -e '.[full]'
python -m paper_fetch.institutional --browser msedge --url 'https://www.sciencedirect.com/science/article/pii/S0044848621007377'
```

`--browser msedge` 使用本机已安装的 Edge；`--browser chrome` 使用已安装的
Chrome。不传参数时使用项目管理的 Camoufox，首次运行需要下载浏览器包。
这些选项只属于这个独立入口，普通 provider 的浏览器配置保持原有行为。

首次使用时，在弹出的北大统一认证页面填写账号和密码。CARSI 可能再要求
确认向 Elsevier 传递身份信息；阅读页面后自行选择“允许”，才能完成跳转。
脚本等待返回目标文章，随后在同一个浏览器会话中打开出版社 PDF 链接。
出版社是否提供文件取决于当前登录状态、订阅范围及开放获取情况。

默认文件保存在 `~/Downloads/paper-fetch/PII.pdf`，旁边的 JSON 记录字节数、
页数、SHA-256 和文章身份。保存前检查文件格式、可解析性，并要求内容包含
目标 PII 或目标文章页提供的 DOI；文件按原始字节保存。
可用参数：`--output-dir PATH`、`--timeout-seconds 600`、`--overwrite`。

`idp_page_observed` 表示脚本看到了北大认证页面；
`institution_label_observed` 表示文章页出现了 Peking University。
这些观察不能单独证明订阅授权，下载到 PDF 也不能单独证明访问来自机构
订阅而非开放获取。两种观察均记录实际情况，不根据下载成功推断认证成功。

浏览器会话默认存放在操作系统的用户数据目录下：
`paper-fetch/institutional/pku-sciencedirect-BROWSER`，可以复用未过期登录态。
该目录包含 Cookie，应保留在仓库外。`--profile-dir PATH` 可指定其他本地目录；
不同浏览器不要共用同一个 profile。密码直接填在认证网页，无需明文
`secret.md`。脚本不保存登录页面 HTML、截图、Cookie 值或带签名的 PDF 链接。
关闭浏览器或按 Ctrl+C 可以取消。

当前支持北大 + ScienceDirect 文章 URL。它独立于 Elsevier API provider，
不改变 `paper-fetch fetch`。本地实测平台为 Windows；没有原生 macOS 实测证据。

若出版社页面停在“请稍候…”或 “Just a moment”，入口会明确提示网站验证尚未
完成。完成可见验证后仍不跳转时，本次运行不能视为已取得订阅访问权；超时
将失败退出。切换浏览器会使用独立 profile，可能需要重新完成北大认证。
