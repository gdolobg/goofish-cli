# Architecture

> 一句话：**Single Registry → CLI / MCP / Skill 三态共享**。加一个新命令只需在 `commands/` 下添一个文件，三种形态自动获得。

参照 [opencli](https://github.com/jackwener/opencli) 的 single registry 思路实现。

## 分层

```
┌─────────────────────────────────────────────────────────────┐
│  形态层（Surface）                                           │
│  ├─ cli.py          Typer → registry 生成子命令树           │
│  ├─ mcp_server.py   FastMCP → registry 注册 @mcp.tool()     │
│  └─ skills/*        SKILL.md（v0.2 规划）                    │
└─────────────────────────────────────────────────────────────┘
                             ▲
                             │ iter_commands()
┌─────────────────────────────────────────────────────────────┐
│  Registry 层                                                 │
│  core/registry.py   @command(...) 注册中心（单例）          │
└─────────────────────────────────────────────────────────────┘
                             ▲
                             │ discover()
┌─────────────────────────────────────────────────────────────┐
│  命令层（Business）                                          │
│  commands/<ns>/<action>.py                                  │
│    auth/    login, status, reset-guard                      │
│    item/    get, publish, delete                            │
│    media/   upload                                          │
│    category/recommend                                       │
│    location/default                                         │
└─────────────────────────────────────────────────────────────┘
                             ▲
                             │ call / acquire / watch
┌─────────────────────────────────────────────────────────────┐
│  Core 层（Infra）                                            │
│  core/sign.py       pyexecjs → goofish_js_version_2.js      │
│  core/session.py    cookie 加载 + requests.Session          │
│  core/mtop.py       统一 mtop 调用 + 错误分类               │
│  core/limiter.py    账号业务桶限流（经营/媒体分开）          │
│  core/guard.py      风控熔断（RGV587 → trip）              │
│  core/output.py     统一渲染 json/yaml/table/md/csv         │
│  core/errors.py     异常体系 + exit_code                    │
└─────────────────────────────────────────────────────────────┘
```

## 关键设计：`@command` 装饰器

每个命令文件长这样：

```python
from goofish_cli.core import Session, Strategy, command
from goofish_cli.core.mtop import call

@command(
    namespace="item",
    name="get",
    description="查询闲鱼商品详情（只读）",
    strategy=Strategy.COOKIE,
    columns=["item_id", "title", "price", "seller_nick", "status"],
)
def get(item_id: str) -> dict:
    session = Session.load()
    raw = call(session, api="mtop.taobao.idle.pc.detail",
               data={"itemId": item_id}, version="1.0")
    ...
```

- **namespace + name** → CLI 路径 `goofish item get`，MCP tool 名 `item_get`
- **columns** → 输出契约（table/csv/md 场景的列顺序）
- **strategy** → 认证要求（PUBLIC / COOKIE / WS）
- **write=True** → 写操作元数据；实际写端点必须进入 `write_operation(session, bucket)`

## 风控护栏

1. **滑动窗口预算**（`core/limiter.py`）：按账号和业务桶计数。发布与下架共用 `item.write`，消息用 `message.write`，默认各 1 次/分钟；上传用独立 `media.write`，默认 9 次/分钟。经营预算通过 `GOOFISH_WRITE_RPM` 配置，媒体通过 `GOOFISH_MEDIA_WRITE_RPM` 配置。这是本机预算，不代表平台额度。
2. **熔断**（`core/guard.py`）：所有写端点先检查账号熔断；命中 `RiskControlError` 后持久化，默认 10 分钟。旧版共享熔断和预算须自然过期。保存失败保留原始风控错误并提示停止写入。
3. **持久化**（`core/local_state.py`）：稳定锁文件覆盖读、检查和原子替换事务；跨 CLI 进程和 MCP 线程共享。损坏状态拒绝写入，不静默清零。可通过 `GOOFISH_LIMITER_PATH`、`GOOFISH_GUARD_PATH` 选择隔离状态路径。
4. **响应识别**：MTop 与上传端点根据真实响应确认成功或拒绝。失败尝试不退预算；结果未知时先回读，不自动重发。

发布可接受本地图片，也可通过 `images_json` 复用 `media_upload` 的完整收据；`category_json` 和 `location_json` 接收相应工具返回的已确认 DTO。准备或提交失败保留收据与准备信息，分别报告 `not_submitted`、`submission_rejected` 或 `submission_unknown`。返回商品 ID 代表 `accepted`，详情与商品列表回读负责确认实际保存。

搜索观察网页实际发出的搜索响应，按 `keyword/pageNumber` 与请求发起代次关联；分页前清除上一页状态，忽略迟到响应。只有平台控制字段确认的查询结果进入 `items`；无命中时网页的推荐卡片单独识别。未标明语义的标签保留在 `labels`，品牌、成色等未知属性返回 null，卖家昵称与地域分开。翻页失败保留部分结果，CLI 非零退出，MCP 返回 `ok=false` 和数据及错误。

搜索排序使用可见的“综合 / 信用排序 / 最近活跃”文字定位入口，不依赖构建生成的 class，也不选择隐藏菜单。切换后仍须观察到匹配 `sortField/sortValue` 的真实搜索响应，失败时明确提示人工检查沙箱页面，不降级为综合排序。平台已明确返回空结果时跳过排序和价格控件操作，正常返回零命中。

排序入口悬停后，先取得目标选项句柄并单独等待 `stable` 状态（最多 5 秒，稳定后立即继续），再点击同一选项，避免展开中的菜单让点击操作进入滚动重试；句柄在结束后释放。失败报错将诊断编号放在开头，防止摘要截断。

排序超时区分“悬停排序入口”和“点击排序选项”两个阶段，并保留原始异常链；最多额外等待 2 秒读取菜单诊断。诊断只记录固定排序文字及其显示、位置、尺寸和指针状态，不读取账号、Cookie 或商品正文；截断到 1600 字符，诊断失败不覆盖原始排序错误。

排序前启动本机事件 trace：最多 160 条、25 秒，记录阶段、指针、焦点、滚动及排序控件状态，包装 `scrollTo/scrollBy/scrollIntoView` 保留去 URL 的调用栈；Playwright/CDP 内部发起的滚动不保证有 JS 调用栈。成功后恢复监听器和原函数，不写文件；失败保留 JSON 轨迹及只显示排序控件的截图（其他页面内容隐藏），不启用可能保存网络凭据的原生 Playwright trace。失败包位于 `~/.goofish-cli/diagnostics/search-sort/`，只保留最近 5 份，错误返回 `sort-<uuid>` 诊断编号；沙箱内该目录属于原登录态持久卷。不保存正文、账号、输入内容、Cookie、网络请求或响应。截图或采集失败不得覆盖原始排序错误。

价格筛选按网页实际请求匹配：只填最高价时闲鱼发送 `priceRange:0,最高价`，缺省最低边界按 0 校验；返回值中的 `min_price` 仍为 null，不把 0 当作用户输入。未填写最高价时继续沿用原格式。

商品详情使用 `itemDO/sellerDO`，不使用埋点推测价格和状态；价格 0 和状态码 0 保留，`defaultPrice` 布尔值只表达议价类型。分类模型分数只从选中分类或分类卡的复合身份获取，不把其他属性分数当置信度，也不把缺失分数当 0。

`item view` 的本机与沙箱路径共用 `read_item_page`：导航前注册目标请求观察器，校验接口、请求商品 ID、业务状态和响应商品 ID，再抽取字段。导航与响应等待共用默认 20 秒的截止时间，不用固定等待或页面内异步 SDK 调用取数。SDK 刷新 token 的中间响应、导航取消的响应体读取可等待后续自然响应，但不会延长截止时间或主动补发请求；会话过期、风控和业务拒绝仍是终止结果。响应按完成顺序排队，后续结果不能覆盖已收到的终止错误。退出时移除监听器并取消未完成读取。沙箱人工验证命令显式允许在同一页面继续等待用户操作。本机 `item get` 的直签 HTTP 路径保持不变。

## 实现要点

- **`t` 毫秒位**：`int(time.time() * 1000)` 取真实毫秒（避免 `int(time.time()) * 1000` 把末三位抹成 000 的精度陷阱）
- **默认地址**：优先 `selectedPoi`，缺失时使用第一个常用地址；发布使用返回的 `selected` DTO，显式地址通过 `location_json` 提供。
- **风控识别**：扫描响应体关键字（`RGV587_ERROR` / `punish` / `FAIL_SYS_USER_VALIDATE`），命中即熔断
- **限流**：滑动窗口预算持久化在 `~/.goofish-cli/limiter.json`
- **形态**：CLI + MCP（+ Skill 规划），同一份 registry 输出三种形态
- **命令组织**：每命令一个文件，`@command(...)` 装饰器自注册，参照 opencli
