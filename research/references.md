# Reference Repositories

项目采用 **Pi-first** 策略。Pi 是主架构基准；其他项目只在对应研究专题中进行对照。默认不追踪远程 main 的变化。

| Repo | Role | Git remote | Research commit |
|---|---|---|---|
| Pi | Main architecture | https://github.com/earendil-works/pi | `7c22a93d0ed02092142fa63823e63d76aa4e2406` |
| OpenAI Agents SDK (Python) | Comparison only | https://github.com/openai/openai-agents-python | 尚未固定；首次专题调研时记录 |
| mini-swe-agent | Comparison only | https://github.com/SWE-agent/mini-swe-agent | 尚未固定；首次专题调研时记录 |
| learn-claude-code | Comparison only | https://github.com/shareAI-lab/learn-claude-code | 尚未固定；首次专题调研时记录 |

## 版本管理约定

1. 参考源码放在 `references/` Git submodule；加入以后，父仓库提交会固定其具体 Commit。
2. Pi 源码研究报告所依据的 Commit **固定不变**；切换主基线时，要记录差异并重新核对相关结论。
3. 每个研究结论注明仓库、Commit、文件和函数；记录哪些测试已阅读、哪些真正运行。
4. 参考项目的设计不同于 Pi 时，优先分析差异，不直接改动本项目的 Pi-first 分层。

更详细的 Pi 核心研究见 [pi-source-study.md](pi-source-study.md)。
