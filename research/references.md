# Reference Repositories

项目采用 **Pi-first** 策略。Pi 是主架构基准；其他项目只在对应研究专题中进行对照。默认不追踪远程 main 的变化。

源码准备日期：2026-10-23。四个仓库已下载到 `references/` 并作为 Git submodule 登记；下表记录本项目固定的版本。

| Repo | Role | Git remote | Pinned commit | 状态 |
|---|---|---|---|---|
| Pi | Main architecture | https://github.com/earendil-works/pi | `7c22a93d0ed02092142fa63823e63d76aa4e2406` | 已下载；沿用 R0 报告的有限审阅范围，本步未新增审阅 |
| OpenAI Agents SDK (Python) | Comparison only | https://github.com/openai/openai-agents-python | `0dc4cf152bff1f50bdc0e41f28808563406545b2` | 已下载，尚未审阅 |
| mini-swe-agent | Comparison only | https://github.com/SWE-agent/mini-swe-agent | `04d809ceab9df28f9adaed044884180159172930` | 已下载，尚未审阅 |
| learn-claude-code | Comparison only | https://github.com/shareAI-lab/learn-claude-code | `ce8f9f186058939da54c9d6fead78dfb5d0fd6c3` | 已下载，尚未审阅 |

## 版本管理约定

1. 参考源码放在 `references/` Git submodule；加入以后，父仓库提交会固定其具体 Commit。
2. Pi 源码研究报告所依据的 Commit **固定不变**；切换主基线时，要记录差异并重新核对相关结论。
3. 每个研究结论注明仓库、Commit、文件和函数；记录哪些测试已阅读、哪些真正运行。
4. 参考项目的设计不同于 Pi 时，优先分析差异，不直接改动本项目的 Pi-first 分层。
5. 其他三个项目首次下载时记录固定 Commit，不将它当成已经研究过的版本；首次专题审阅时补充具体覆盖范围与证据。

## 子模块如何保存参考版本

`.gitmodules` 保存远程地址与本地路径。主仓库使用模式为 `160000` 的 gitlink 条目记录子模块 Commit，不复制第三方仓库的全部文件进自己的提交。子模块目录中保留可阅读、检索的源码和独立 Git 历史。

在项目根目录检查：

```bash
git submodule status
git ls-files --stage references/
```

第一条命令显示实际检出的版本；其行首空格表示与主项目索引记录一致，`+` 表示不一致，`-` 表示未初始化。第二条命令显示主项目索引记录的版本，准备提交时应与上表、子模块 `HEAD` 一致。

克隆本项目后使用 `git submodule update --init --recursive` 恢复所记录的版本。更新参考版本应单独记录理由与差异，不默认使用 `--remote` 跟随上游。源码下载不需要安装参考项目的依赖，本步未运行它们的测试。

更详细的 Pi 核心研究见 [pi-source-study.md](pi-source-study.md)。
