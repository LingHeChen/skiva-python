# Skiva Python SDK

[English](https://github.com/LingHeChen/skiva-python/blob/main/README.md) · 简体中文

基于 Skiva `/distribution/v1` 协议的异步 Python SDK，要求 Python 3.11+。安装包名 `skiva-sdk`，导入名 `skiva_sdk`。当前版本：0.3.0。

[GitHub 仓库](https://github.com/LingHeChen/skiva-python) · [Skiva 服务端](https://github.com/LingHeChen/skiva)

## 安装

从 PyPI 安装：

```bash
python -m pip install skiva-sdk==0.3.0
# 可选框架适配，已针对 Pydantic AI 2.59.0 验证：
python -m pip install 'skiva-sdk[pydantic-ai]==0.3.0'
```

使用**启用的 Agent Profile 所属客户端凭据**，不能使用管理员或登录会话令牌。地址填写服务端地址或反向代理前缀，不包含 `/distribution/v1`。可信本地环境之外使用 HTTPS。

```bash
export SKIVA_SERVER_URL=http://localhost:8080
export SKIVA_CLIENT_TOKEN=your-client-credential
```

Profile 需要有已发布快照及 Skill 访问权限。SDK 的动态模式仍遵守服务端 Profile 的固定版本配置，不绕过权限。

## 发现与加载

```python
import asyncio
from skiva_sdk import SkivaClient

async def main():
    async with SkivaClient.from_env() as client:
        async with client.bind() as skills:
            catalog = await skills.catalog()
            print(catalog.render())
            if catalog.skills:
                content = await skills.load_skill(
                    catalog.skills[0].name,
                    expected_snapshot_id=catalog.ref.snapshot_id,
                    expected_catalog_digest=catalog.catalog_digest,
                )
                print(content.render())

asyncio.run(main())
```

`Catalog` 包含 `SnapshotRef`、不可变 Skill 元数据与 `catalog_digest`。`SkillContent` 包含实际 ref、目录摘要、清单相对路径、文件摘要和正文。输出包裹结构会转义 XML，但名称和描述仍来自 Skill 发布者；转义不意味着它们可信，更不意味着可以覆盖系统策略。

发现提示词要求模型按相等性比较版本，不比较版本号大小；不一致时重新加载。回滚与向前更新采用相同行为。同一快照下可见文件集合变化也会改变 `catalog_digest`。这是面向模型的降级提示机制，**不保证模型忘记旧指令**。

## 选择接口

| 接口 | 行为 |
|---|---|
| `client.bind()` | 默认每次访问检查当前授权版本 |
| `client.snapshot()` | 固定进入时的当前快照 |
| `client.snapshot("review-42")` | 按业务 ID 记住并复用快照 |
| `client.snapshot(source=ref)` | 固定指定的实际快照引用 |

推荐使用上述两个主接口。`consistency="pinned"` 与显式 `source` 的 `bind()` 保留为底层兼容配置；不需要同时理解两套使用方式。`client.task(id)` 只是 `snapshot(id)` 的别名，不是另一种模式。

## 固定与动态策略

```python
async with client.bind(consistency="dynamic") as skills:
    await skills.refresh()
    async with skills.snapshot() as batch:
        body = await batch.read_text("review/SKILL.md")
        rules = await batch.read_text("review/references/rules.md")
        original_ref = batch.ref
```

- 可选 `pinned`：绑定期间固定快照与过滤清单，通道变化不影响当前版本。
- 默认 `dynamic`：访问时按 `refresh_interval` 检查更新，默认 0 秒，即每次访问检查更新；`refresh()` 强制刷新。没有后台守护任务或 SSE 订阅。
- 带预期版本的动态加载会强制检查更新，不等待轮询间隔。快照或目录摘要不一致时抛出 `VersionMismatch`，不静默替换。
- 新视图完整下载校验后原子发布。单次操作捕获一个版本，不同操作可能跨版本。`snapshot()` 为关联读取固定视图，即使外围动态会话继续更新也保持不变。
- 刷新失败会使该操作失败，并设置 `last_refresh_error`，不把旧目录冒充最新目录。成功刷新后清除错误，`transition_count` 记录成功切换次数。
- 可以设置客户端默认策略，并在绑定时覆盖。显式 `source=SnapshotRef` 在未指定策略时自动选择固定策略，与显式动态策略组合会被拒绝，可用于明确的原版本重试，但仍需满足当前服务端授权。
- 动态模式的 `view.ref` 只代表观察瞬间。追踪实际读取来源应使用返回的 `SkillContent.ref` 或 `batch.ref`。

任务和执行 ID 仅作关联，可省略并自动生成。只在**同一个客户端实例内**拒绝重复的活动 ID。没有持久化执行存储、自动崩溃恢复或跨进程恰好执行一次保证。应用可自行持久化 ref，用于显式固定版本重试；ref 不授予权限，也不能绕过新凭据的权限。

## 命名快照绑定

```python
# Default: check the current authorized target on each access.
async with client.bind() as skills:
    catalog = await skills.catalog()

# First entry pins the current snapshot; re-entry reuses it.
async with client.snapshot("review-42") as skills:
    catalog = await skills.catalog()

# Explicitly replace the binding with the latest authorized snapshot.
# This new view stays fixed throughout this block, too.
async with client.snapshot("review-42", retry_policy="latest") as skills:
    catalog = await skills.catalog()

client.forget_snapshot("review-42")
```

`client.snapshot(id)` 是固定视图的主接口。字符串是**业务绑定 ID**，不是服务端快照 UUID。`client.task(id)` 仅为别名。`client.snapshot()` 不传 ID 时创建匿名固定视图；指定实际快照引用使用 `client.snapshot(source=ref)`。

退出上下文或业务抛出异常后仍保留绑定，因此同 ID 重试默认复用原快照。`retry_policy="latest"` 仅在新快照准备成功后替换记录，而且整个代码块内依然固定新版本，不会动态切换。每次访问仍检查当前权限；权限被拒绝或视图变化时明确失败，不静默改用最新版本。

未配置 `binding_store` 时，映射**只保存在当前客户端实例内**。跨请求复用同一个长期存活的 client；不同 client、进程或重启不共享绑定。跨重启恢复时由应用保存 `SnapshotRef`，再传入 `source=ref`。同 ID 的并发上下文抛出 `SnapshotBusy`；并行读取可以共享已经打开的视图。默认最多保存 1,024 个 ID（`max_snapshot_bindings`）；业务结束后调用 `forget_snapshot(id)`。活动绑定不能清理；清理只删除本地映射，不删除服务端快照或授权。没有可能使重试版本悄悄变化的自动淘汰或过期。

## 可选持久化绑定

```python
from skiva_sdk import SkivaClient, SQLiteBindingStore

store = SQLiteBindingStore("bindings.sqlite", namespace="my-app/review-profile")
async with SkivaClient.from_env(binding_store=store) as client:
    async with client.snapshot("review-42") as skills:
        catalog = await skills.catalog()
        print(catalog.render())
    # A later process using the same database/namespace recovers this binding.
    # Explicit cleanup after the application no longer needs retries:
    # client.forget_snapshot("review-42")
```

默认使用客户端实例内存。选择 `SQLiteBindingStore` 后，业务 ID 可跨实例、进程和重启恢复，仍需每次在线授权。命名空间应对应应用与 Profile；不同服务器自动分隔。使用受保护的本地磁盘目录，不要使用网络文件系统。数据库只保存引用，不保存凭据或 Skill 正文。

存储使用原子比较更新，准备期间发现其他进程改写绑定时抛出 `SnapshotBusy`，不会静默覆盖。同 client 内禁止同 ID 重叠上下文；跨 client/进程可以同时使用已经固定的视图，这不是分布式任务执行锁。`latest` 或删除记录不影响已经打开的视图；应用应在所有相关执行完成后清理。没有自动过期；容量按存储命名空间计算。自定义存储可实现 `BindingStore` 协议中的原子 `get/put/delete`，方法应是快速的本地元数据操作。

清单使用有界的 ETag 缓存（最多 16 项且总字节不超过 `max_manifest_bytes`），每次仍请求服务器授权；网络错误或撤权不会回退到缓存。固定快照不是离线模式。

## 重试策略与更新策略独立

0.2.0 将客户端默认策略从 pinned 改为 dynamic。需要普通绑定固定版本的现有应用，请显式传入 `consistency="pinned"`。重试策略独立配置为 `retry_policy="reuse_snapshot"`（默认）或 `"latest"`，可设置客户端默认值，也可在 `client.retry()` 中覆盖。

```python
async with client.bind(task_id="review-42") as skills:
    catalog = await skills.catalog()
    # 使用当前会话执行应用或 Agent。

checkpoint = skills.retry_checkpoint

# 默认：整个重试期间固定检查点中的快照。
async with client.retry(checkpoint) as retry:
    print((await retry.catalog()).render())

# 可选：获取当前授权目标，再遵循客户端更新策略。
async with client.retry(checkpoint, retry_policy="latest") as retry:
    print((await retry.catalog()).render())
```

`retry_checkpoint` 是不可变的 `RetryCheckpoint(task_id, attempt_id, snapshot)`，记录会话**最后一次完整切换成功的视图**，不一定是首次绑定的版本。退出绑定后仍可获取，包括应用抛出异常的情况。若需复用某个特定视图，应在后续更新之前保存检查点。刷新失败或取消不会推进检查点。动态执行可能读取过多个版本，一个检查点不能重现整个读取序列或模型历史。若要重试某次返回内容或批次的准确版本，可使用该 `SkillContent.ref` 或 `SnapshotView.ref`，结合会话的任务与执行 ID 构造检查点。

`client.retry(checkpoint)` 保留任务 ID、生成新的执行 ID、重新校验当前权限，并在整个重试期间固定检查点快照。权限拒绝或清单变化时失败，不自动降级使用最新版本。`retry_policy="latest"` 获取当前 Profile 授权目标（仍遵守服务端固定配置），之后使用客户端更新策略，默认为动态。每次重试也可以生成检查点，用于下一次重试。检查点没有凭据，不授予权限；应用可通过 `dataclasses.asdict` 持久化，恢复时重建嵌套的 `SnapshotRef`。

使用相同任务 ID 再次调用 `bind()` 不会自动推断为重试。SDK 不会自动重放业务动作或重试框架工具，需由应用在自己的重试边界调用。应用异常时，在退出 `bind` 后的异常处理器中读取 `skills.retry_checkpoint`，再显式调用 `client.retry`。首次绑定准备失败时没有可用检查点。选择最新版本会重新加载 SDK 内容，但不会清除历史消息；是否新建模型 run 或上下文由应用决定。

`SkivaSkills` 同时接受 `SkillSession` 和 `SnapshotView`。示例使用命名固定视图；需要动态更新时改用 `deps_type=SkillSession` 和 `client.bind()`。固定视图会重新检查权限，但不切换版本。

## Pydantic AI 集成

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Caching
from skiva_sdk import SnapshotView
from skiva_sdk.integrations.pydantic_ai import SkivaSkills

agent = Agent(
    model,  # 应用配置的 Pydantic AI 模型或供应商实例。
    name="skiva-agent",
    deps_type=SnapshotView,
    capabilities=[Caching(), SkivaSkills()],
)

async with agent:
    async with client.snapshot("review-42") as skills:
        result = await agent.run("审查这次修改", deps=skills)
```

`SkivaSkills` 提供具名动态指令块，使用 **rewrite** 语义在**每次模型请求前**刷新发现列表，绕过普通轮询间隔。同时注册 `load_skill`、`read_skill_file`，要求传入两个预期版本字段。版本变化或未找到转换为结构化工具结果；权限和网络错误直接中止，不鼓励沿用旧内容。不会删除历史工具返回或派生结论。指令在供应商请求中的具体位置由 Pydantic AI 和供应商决定。

通过 `SkivaSkills(get_session=lambda deps: deps.skills)` 保留业务依赖。每个 run 应使用独立会话；适配器不保存跨 run 的可变状态。一个 Agent 只挂载一个 SkivaSkills（工具名称固定）。这是独立发现和加载工具集成，**不是**原生 Harness `Skills` 的工作区后端，也不拦截 shell 或直接文件访问。追踪由宿主应用配置，SDK 不向外部遥测服务发送提示词或令牌。

发现和读取共享一个绑定。普通绑定默认动态更新；显式固定绑定及复用快照的重试始终展示固定目录。已发出的模型请求不会被修改，更新指令前缀可能影响提示词缓存。

## 需要真实文件时

```python
async with skills.snapshot() as batch:
    async with batch.materialize() as directory:
        # 将目录交给框架或子进程，子进程必须在退出此代码块前结束。
        print(directory)
```

SDK 在临时目录导出完整私有副本，保留读取和执行权限位，不创建 FUSE 挂载或共享对象的可写硬链接。拒绝不适合支持平台的路径，包括大小写折叠冲突。退出时清理。权限位不能隔离拥有文件的进程。**导出后的直接读取绕过 SDK 授权，撤权无法收回导出的字节。** 新快照需重新导出，不修改旧目录。

## 校验、权限与资源限制

视图发布前完整下载并校验 Go 兼容清单摘要、SHA-256、大小、路径和权限位。仅使用视图私有内存缓存，没有共享磁盘或离线缓存。默认上限：10,000 个文件，单文件及清单各 8 MiB，单快照 64 MiB，SKILL.md 解析输入 256 KiB。并发和保留旧视图会叠加内存，应用应限制并发并释放引用。

每次公共读取、目录获取、导出均在线重新检查指定快照清单，即使内容已缓存。权限变化使捕获视图失效，已知客户端撤销在本客户端实例内持续生效。网络失败即阻止操作。不提供离线模式、透明网络重试或业务任务重试。最后检查之后的鉴权竞态和已交付内容无法撤回。不跟随 HTTP 重定向，SDK 错误消息不包含令牌；自行开启的底层网络日志仍可能暴露凭据，需要正确配置。默认忽略环境代理和 CA 配置（`trust_env=False`），需要时显式设置 `trust_env=True`；SOCKS 代理还需安装 `httpx[socks]`。也可传入自定义 HTTPX transport 配置 TLS 或代理。

主要错误：`VersionMismatch`、`ManifestChanged`、`AuthorizationDenied`、`IntegrityError`、`ProtocolError`、`LimitExceeded`、`SkillNotFound`、`TransportError`、`ClosedError`。

## 开发与验证

```bash
uv sync --extra dev --extra pydantic-ai
uv run pytest -q
uv run ruff check src tests examples
uv run python -m build
uv run python examples/offline_demo.py
```

测试使用受控分发服务和真实 Pydantic `FunctionModel` 执行循环，不调用外部模型。覆盖更新、回滚、同快照权限变化、并发读取和执行、历史内容版本、损坏、目录隔离及 Go 生成的清单兼容样例。离线演示不需要服务端或凭据；不能替代与实际部署和模型供应商的联调验收。

参见 [examples/](https://github.com/LingHeChen/skiva-python/tree/main/examples/) 和 [CHANGELOG.zh-CN.md](https://github.com/LingHeChen/skiva-python/blob/main/CHANGELOG.zh-CN.md)。官方集成依据：[Pydantic capabilities](https://pydantic.dev/docs/ai/capabilities/custom/)、[Agent Skills 实现指南](https://agentskills.io/client-implementation/adding-skills-support)。

完整场景和结果记录：[测试指南](https://github.com/LingHeChen/skiva-python/blob/main/docs/TESTING.zh-CN.md)。
