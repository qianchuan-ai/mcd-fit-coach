# MCP_INTEGRATION —— 麦当劳 MCP 接入说明

本文档说明 `mcd-fit-coach`（麦门私教）如何接入并使用麦当劳官方 MCP 能力。

## 1. 连接信息

| 项 | 值 |
|---|---|
| 协议 | MCP（Model Context Protocol），Streamable HTTP 传输 |
| 地址 | `https://mcp.mcd.cn`（可用环境变量 `MCD_MCP_URL` 覆盖） |
| 认证 | HTTP Header `Authorization: Bearer <MCP Token>` |
| Token 获取 | [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 右上角「登录」（扫码或手机号验证，登录即开通，无独立注册环节）→「控制台」→「激活」→ 同意协议 → 复制 Token |
| 协议格式 | JSON-RPC 2.0；响应兼容 `application/json` 与 `text/event-stream`（SSE）两种形态 |

连接配置示例见 [mcp-config.example.json](mcp-config.example.json)。

## 2. 客户端实现（`fit_coach.py` 内 `McpClient` 类）

零第三方依赖，仅用 Python 标准库 `urllib` 完成完整 MCP 握手与调用：

```
POST initialize (protocolVersion 2025-03-26)
  → 读取响应头 Mcp-Session-Id 建立会话
POST notifications/initialized
POST tools/list
  → 获取服务端全部工具（name / description / inputSchema）
POST tools/call { name, arguments }
  → 解析 result.content[].text（逐层解包 JSON 字符串）
```

SSE 兼容：若服务端返回 `text/event-stream`，按 `data:` 行逐段解析 JSON 事件。
会话全程复用 `Mcp-Session-Id`，Token 只从环境变量 `MCD_MCP_TOKEN` 或 `--token` 读取。

## 3. 使用的工具（与官方 `mcd-mcp-server` 指南对照）

麦当劳 MCP Server 当前开放 32 个工具（覆盖点餐、领券、活动、商城、派对等场景）。
本项目遵循最小权限原则，只调用以下工具：

| 工具名 | 官方名称 | 本项目用途 |
|---|---|---|
| `list-nutrition-foods` | 餐品营养信息列表 | 主数据源：获取常见餐品的能量、蛋白质、脂肪、碳水化合物等营养成分，驱动配餐引擎 |
| `available-coupons` | 麦麦省券列表查询 | 获取当前可领取的优惠券，展示在餐单卡片备注 |
| `query-my-coupons` | 我的优惠券查询 | 兜底：查询账户已有可用优惠券 |

调用策略：`tools/list` 动态确认服务端工具存在后，营养工具优先精确调用
`list-nutrition-foods`，失败则按名称/描述模糊匹配（`nutrition`/`food`/`menu`/「营养」）
降级尝试；优惠券工具按 `available-coupons` → `query-my-coupons` → 模糊匹配的顺序获取，
失败仅降级为备注提示，不阻断主流程。

营养字段提取容错：热量按 `calorie(s)/kcal/energy/热量/卡路里` 匹配，蛋白质按
`protein/蛋白` 匹配，名称按 `name/foodName/title/名称` 匹配，并自动剥离
「千卡/kcal/克/g」等单位后缀。

不触碰的能力：本项目不调用 `create-order`/`cancel-order`/`mall-create-order`/
`draw-lottery` 等下单、支付、消耗积分类工具，只读不写。

## 4. 业务流程

```
用户画像（身高/体重/年龄/性别/活动水平/目标）
        │
        ▼
热量引擎：BMR → TDEE → 目标热量与蛋白质目标
        │
        ▼
麦当劳 MCP：tools/list 发现 → tools/call 获取菜单营养数据（+ 优惠券）
        │
        ▼
配餐引擎：早30%/午40%/晚30% 贪心选择 + 加餐补齐（预算 ±8%）
        │
        ▼
产出：终端餐单 + 单文件 HTML 分享卡片（mock 模式带演示水印）
```

## 5. 业务价值

- 把麦当劳 MCP 的**营养数据能力**延伸到「健康生活方式」场景：减脂人群可以在
  麦当劳的点餐决策中直接获得「能不能吃、吃多少」的量化答案；
- 用官方数据而非拍脑袋写死菜单，餐品与营养数值随服务端数据更新；
- 产出物天然可分享，为麦当劳 MCP 生态带来传播截面；
- 对开发者而言，`McpClient` 是一份 60 行、可读、可复用的 MCP Streamable HTTP
  参考实现。

## 6. 服务端能力与限制（摘自官方 mcd-mcp-server 指南）

- 官方指南仓库：https://github.com/M-China/mcd-mcp-server
- 限流：每个 Token 每分钟最多 600 次请求，超限返回 429；Token 无效/过期返回 401。
- `python fit_coach.py --list-tools` 可实时打印服务端当前全部工具，与官方清单对照。

## 7. 首次真实运行记录（2026-10-25）

本节为实际连接 `https://mcp.mcd.cn` 的运行记录，可作为社区接入参考。
官方在线文档：https://open.mcd.cn/mcp/doc

- `tools/list` 实测返回 **35 个工具**（较 GitHub 文档 32 个新增 `query-survey-coupon`、
  `query-promotions`），本项目用到的 `list-nutrition-foods`、`available-coupons`、
  `query-my-coupons` 均在线；三者按官方文档均**无需入参**；
- **响应格式**：`list-nutrition-foods` 为降低 LLM Token 消耗，采用官方所称
  **TOON（Token-Oriented Object Notation）紧凑格式**而非标准 JSON 数组：外层是
  markdown 信封（`# API Response Information` 说明段 + `## Original Response`
  中的 JSON 对象），`data` 字段为首行 `[行数]{列名1,列名2,...}:`、随后每行逗号
  分隔记录的紧凑表格。本项目的 `unwrap_api_response()` 与 `parse_custom_table()`
  即为此格式的解析实现，与官方文档示例逐字段一致；
- `list-nutrition-foods` 实测返回 **160 个餐品**，列含
  `productName, nutritionDescription, energyKj, energyKcal, protein, fat,
  carbohydrate, sodium, calcium`，本项目取 `energyKcal`（千卡）与 `protein`（克）；
- `available-coupons` 实测成功返回券列表（markdown 文本，含券标题与领取状态），
  用于卡片备注展示；
- 端到端验证：真实模式生成餐单合计 1979 kcal / 预算 2032 kcal（误差 2.6%），卡片
  右上角标注「MCP 实时数据」（见 `demo/card-preview.png`）；mock 模式卡片则标注
  「演示数据 · MOCK」，两种模式绝不混淆。

## 8. 错误与降级策略

- 连接失败 / Token 缺失：明确报错并提示使用 `--mock`；
- 营养工具调用失败：回退演示数据，卡片盖「演示数据」水印并在终端说明原因，
  绝不把演示数据冒充为真实 MCP 数据；
- 优惠券等非核心能力失败：仅降级为备注提示，不阻断主流程。
