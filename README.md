# 启航 · 训练营指标口径签核

把经营指标从口头约定变为可试算、可比较、可签核冻结的版本记录。

## 真实业务场景

启航训练营经营分析组服务四个线上/线下班级，每天收到班主任的 `启航_班级日报_20261115.xlsx`。原流程由分析员在工作簿计算实到率、结课达成率、退款占比，再把数字抄入经营会纪要。痛点是分子分母的过滤范围不一致、修改公式后旧结论失去依据、数据更正与口径变化无法分清。本项目接收上游班级日事实，替代口径登记/试算/差异解释，给下游经营负责人提供冻结依据。

三个业务实体：指标定义Metric（代码+版本唯一）、班级日事实Fact（班级+日期唯一）、试算签核快照Snapshot。四页是口径目录、事实校验、口径试算和签核历史；另有登录。签核历史页还能对两次签核做归因，拆出口径与各班日报对指标差值的贡献。后台worker真实计算，签核锁定口径与意见，后续事实修改不改变历史快照。

|角色|权限|典型动作|
|---|---|---|
|admin|全部业务、审计、独占签核|冻结经营依据|
|analyst|口径/事实读写、试算、业务解释、导出|建立净实到结课率v2并比较v1|
|viewer|业务只读及导出|下载签核快照，后端拒绝写入和任务提交|

## 快速启动

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 [localhost:18210](http://localhost:18210)，后端 [localhost:19210/docs](http://localhost:19210/docs)，PostgreSQL端口17710。账号为 `admin@demo.local` / `analyst@demo.local` / `viewer@demo.local`，密码均 `Demo123!`。`SEED_DEMO=true`启动自动seed，手动重复不会新增同自然键或覆盖人工修订。种子为四班30日共120条事实（2026-10-17至11-15）、三类口径v1和三账号。

## 一天工作剧本

1. 08:50 viewer查看四班的报名/实到/结课/退款人次。
2. 09:00 analyst核对QH-01的2026-11-15事实，满足结课≤实到≤报名、退款≤报名。
3. 09:10 在事实校验录入新的班级日事实；修订时填写记录ID及完整字段。
4. 09:20 对completion v1（completed / attended）提交2026-10-17至11-15试算，刷新得到基线。
5. 09:30 在口径目录创建completion v2，分子completed，分母attended-refunds，写明“净实到”业务解释。
6. 09:40 新建v2试算，选择v1成功快照作为基线，核对比值差异、变更字段、日期窗和事实checksum。
7. 10:00 在签核历史填写不少于8字的解释，说明是否同时存在事实变化。
8. 10:10 admin签核；该版本不可覆盖。10:20 viewer下载 `camp-metric-<id>.json`。

## 指标计算与安全白名单

允许字段为 enrolled/attended/completed/refunds，分别是班级日的报名/实到/结课/退款人次。表达式仅允许数字、字段、括号、+ − * /和一元正负；AST逐节点解释，禁止函数、属性访问、下标、幂运算、布尔、SQL和任意代码执行。表达式最多200字符/60个AST节点，常量绝对值≤1000000；非有限值、行级除零、极小分母造成超限结果显式失败。

按日期窗过滤事实，再按分子与分母各自的授课方式过滤；每行执行对应表达式，分子求和/分母求和，得到ratio。页面乘100展示百分比；不是日比率的简单平均。例如两班结课6/5、实到8/10，合计为11/18；v2扣除退款1/2后是11/15，绝对差11/90。分子分母过滤不同可能超过100%或为负，需人工解释，不自动截断。

外层总分母为0，value=null并显示undefined，不伪装0，可导出诊断但禁止签核。没有事实、非法行表达式使任务failed，修正后重新试算。每次冻结口径、事实、基线、日期窗及checksum。版本比较仅允许同指标代码，显示字段变化、绝对比值差和日期/事实是否一致；数据改变时不能把全部差异归因于口径。

签核需要已成功、非undefined、业务解释至少8字且口径仍与试算一致；试算后修订口径必须重算。签核后锁定口径版本和意见，事实更正不改旧快照。已有冻结版本可再次以新事实试算，但仍保留各次输入。

## 两次签核归因

签核历史页可选择同指标代码、同日期窗的两次已签核快照（基线、当前），发起归因，查看各因素对差值（pp）的贡献与计算依据。归因只读取两次签核快照中冻结的口径和班级日报，不回读现行口径或现行事实；即使之后口径或事实再改，重试归因仍用原签核。

口径与每个发生变化的班日报各算一个因素：因素取“当前”时用当前签核版本、取“基线”时用基线签核版本，其余因素保持对应版本，逐行套用白名单表达式后分别汇总分子/分母相除。贡献是全部替换顺序下边际影响的平均（Shapley 值），因此与替换顺序无关，合计恒等于实际比值差（结果给出残差供核对）；未变化的班贡献为 0。只改名称或版本号、不改变表达式的口径字段不作为因素，但仍列在计算依据里。

因素数不超过 12 时穷举全部 2^n 组替换并逐个校验，结果精确。班级更多时用固定种子的置换抽样估算，给出每项 95% 置信误差界、抽样次数、已访问组合数与校验范围；端点仍用冻结数据精确重算，贡献合计仍等于实际差。任何混搭组合无法算出比值（行级除零、外层分母为零、超限）时整体失败，明确列出出错的组合（哪版口径、哪些班换成当前版），此时不给出任何贡献。归因排队后中断，worker 启动时按原签核重新入队；同一对签核重复提交返回原记录，不新增归因；失败记录可原地重试。计算完成后可查看并导出 `camp-attribution-<id>.json`。

## 工程、本地开发与范围

|层|技术|
|---|---|
|前端|原生ES modules/HTML/CSS，Nginx1.27|
|后端|Python3.11/FastAPI/SQLAlchemy/PyJWT|
|异步|Redis7队列，独立Python worker，心跳健康检查|
|数据库|PostgreSQL16，命名数据卷|

backend等待db/redis健康，worker再等backend，frontend等backend；恰好五服务。数据库、Redis及共享`/artifacts`结果卷持久化。Nginx `/api/`代理到后端根路由。清理用 `docker compose down -v --remove-orphans`。显式网络10.253.111.0/24规避当前机器默认地址池耗尽，可用NETWORK_SUBNET改。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO、NETWORK_SUBNET。演示凭据/密钥部署时替换。本地开发先起db/redis，backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET后 `uvicorn src.main:app --reload`；前端保留同源Nginx或开发代理。

目录按backend/src的routes/controllers/services/models/schemas/middlewares/config/utils及frontend/src的api/stores/pages/components/hooks/utils/constants/types/router拆分。共享表格、字段、状态标签；表单hooks与HTML转义；请求ID/安全头中间件、业务审计和全局错误处理。枚举ROLES/STATES/MODES/APPROVALS/FIELDS位于后端constants/enums.py、schemas/business.py和前端constants/enums.js。ORM建初表（启动时补齐缺失表，含 `attributions`），migrations/001_initial.sql保留实际DDL；无在线版本迁移执行器。归因服务在 `backend/src/services/attribution.py`，结果持久化到 `attributions`，与快照共用 worker 队列和 `/artifacts` 卷。

小型范围：逐条表单/API录入，无Excel批导、删除、分页或跨指标引用；单worker，启动恢复待处理任务。班级日事实是人次，不能解释为去重学员留存。签核只在站内，不外发邮件/电子签章。Google Fonts依赖网络，离线回退serif/sans-serif。测试证据在 `../../test-artifacts/gb-3111/TESTING.md`，源码无截图/日志。MIT License见LICENSE。
