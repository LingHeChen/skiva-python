# SDK 场景测试

[English](TESTING.md) · 简体中文

## 运行

```bash
uv sync --extra dev --extra pydantic-ai
uv run pytest -q --cov=skiva_sdk --cov-fail-under=90 \
  --cov-report=term-missing --cov-report=json:.test-results/coverage.json \
  --junitxml=.test-results/junit.xml
uv run ruff check src tests examples
```

报告、覆盖率数据和 Hypothesis 状态均已加入忽略规则。GitHub Actions 配置 Python 3.11/3.12/3.13，要求语句与分支综合覆盖率至少 90%，并保留 JUnit 和覆盖率产物。覆盖率用于回归门禁，不代表生产可用认证。

## 可查看的复杂 Skill

[cases 目录](../tests/cases/) 包含三组 Skill、两个发布版本，共 36 个文件。业务规则是合成数据，但目录和引用结构贴近实际使用，不构成财务、安全或部署建议。

| Skill | 文件和引用 | 两个版本的预期差异 |
|---|---|---|
| `expense-audit` | 多行 YAML 描述、政策与汇率 JSON、嵌套中文地区文件、票据、报告模板、Python 计算脚本 | 相同票据合计 800；版本一上限 1000，批准；版本二上限 600，拒绝 |
| `release-review` | 检查清单、嵌套安全指南、发布流程、决策模板 | 不兼容变更从 `warning` 改为 `blocking` |
| `data-contract` | 版本化 Schema、CSV、NFC Unicode 文件名、二进制和空文件 | 版本二增加必需字段 `consent`；二进制和空文件仍可按字节读取 |

测试实际执行了这里编写的计算脚本，使用临时导出目录及隔离模式 Python 解释器，并等待进程结束后清理。不会执行部署命令或外部业务写入。

## 验证层次

| 层次 | 测试文件 | 验证内容 |
|---|---|---|
| 现有协议与 HTTP 基线 | `test_sdk.py`、`test_http.py`、`test_protocol_fixture.py` | 基本策略、真实 socket HTTP、Go 生成的规范化样例 |
| 复杂发布场景 | `test_complex_cases.py` | 嵌套读取、脚本实际结果、回滚隔离、Profile 固定策略、凭据授权、权限变化、取消与恢复 |
| 异常输入 | `test_adversarial.py` | 非法 YAML/类型/路径、符号链接条目、畸形清单和拒绝响应、流式断连、超限响应、各项配额、不安全导出文件名 |
| 自动生成序列 | `test_properties.py` | 40 组确定性 Hypothesis 样例，每组 1–20 次发布/回滚，与独立版本状态预期比较 |
| 真实 Agent 执行循环 | `test_pydantic_ai.py`、`test_agent_scenarios.py` | 逐请求更新目录、版本错误、历史来源、并发 run、旧历史恢复、权限变化、流式输出和撤权 |

并发检查包括 24 次同时刷新（只允许一次实际版本切换）和发布后的 24 次读取。另有 500 个 Skill、1,000 个文件的大目录，验证共享引用去重：实际只下载 501 个对象，而不是 1,000 个。这些测试验证正确性，不是吞吐或延迟基准。

多轮 Agent 场景包含八次模型请求：加载两个 Skill、在引用读取前切换发布、拒绝旧版本读取、重新加载并检查新规则、回滚、拒绝原本当前的版本，再重新加载回滚版本。历史中两个快照的正文都仍带版本标记；测试不会假装旧上下文已被删除。

## 测试设施与边界

`tests/scenarios.py` 模拟不可变发布、凭据对应的 Profile、过滤清单、历史授权与 Profile 固定版本。在 await 边界注入确定性故障，避免依赖 sleep 碰运气。它是测试替身，不是真实 Go 服务。已有 socket 测试对接本地测试 HTTP 服务；Go 样例只验证序列化兼容。

Agent 测试使用真实 Pydantic AI 2.59.0 执行循环和预设决策的 `FunctionModel`，无需外部模型、付费调用、API 密钥或真实 Skill 数据。它们验证适配器编排与工具结果，不证明实际大模型一定遵守过期提示，也不认证 Agno/LangChain 兼容性。

后续部署验收仍需：真实 Skiva 后端鉴权、实际 Profile 更新和历史授权、TLS/代理、部署负载下的进程和网络故障、选定的模型供应商、沙箱及操作系统行为。本地 macOS 结果不能证明 Windows 兼容；CI 配置了 Linux，但只有实际运行工作流后才算执行过。

## 本地结果记录

2026-10-22，macOS ARM64、Python 3.12.10：**102 项通过**。语句与分支综合覆盖率 **95.40%**（语句 523/542，分支 120/132）。Python 3.13.5 的独立环境也通过了全部 102 项测试。Python 3.11 本地尝试在依赖下载阶段停滞后终止，尚无该版本的测试结果，CI 矩阵仍保留该版本。目录解析与渲染模块的实测综合覆盖率为 100%。这些数字对应当前版本，不是长期保证。

新增测试发现了一个实际缺陷：固定绑定在刷新失败后，后续成功刷新没有清除 `last_refresh_error`。已修复为成功时清除错误并记录检查时间，固定和动态两种策略均有恢复回归测试。
