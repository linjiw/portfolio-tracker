# 动量研究公开站

这是一个无框架、无外部运行时 API 的静态站。它与私人投资 dashboard 完全分离，
只能读取同目录下的 data/study.json。

首页的“现在该做什么”行动板会同时展示六组策略的公开模型篮子、现金权重、
当前动作、新资金门禁、下一触发条件与风险线。点击任一策略可直接定位到其最新
有效决策；回放节点使用固定语义色区分进入/加仓、持有/观察、换仓/参考与
减仓/退出/阻断。所有“持仓”均指模型目标，不是账户头寸。

## 数据边界

公开数据采用默认拒绝策略：

- public-study.schema.json 对所有对象设置 additionalProperties: false。
- app.js 在渲染前再次执行相同字段 allowlist 和隐私检查。
- 未知字段、账户/交易类字段、本地绝对路径或电子邮件会使页面停止渲染。
- 图表中的“模型暴露”和“双轨模型”是公开策略状态，不是账户仓位。
- `currentSnapshot` 只允许公开模型篮子、现金权重、门禁与执行阈值；每组快照的
  模型篮子加现金必须等于 100%。
- 每个历史决策必须带有受限枚举 `kind`，以保证页面和图表使用同一套语义颜色。
- data/study.json 由严格 allowlist 生成，包含六组公开研究路径；Top3/Top5 始终标记为非决策级代理。

不要复制、软链接或转换整个 output、outputs、broker CSV、dashboard HTML、日志、
manifest、数据库或账户审计工作簿。公开生成器必须从空对象开始，仅写 schema
明确列出的聚合字段。

## 本地查看

从仓库根目录运行：

    python3 -m http.server 8000 --directory site/momentum-research-public

然后打开 http://localhost:8000/。直接使用 file 协议会被前端主动拒绝，以确保
本地和 GitHub Pages 使用相同的同源数据约束。

## 验证

测试只依赖 Python 标准库，也可以由 pytest 收集：

    python3 site/momentum-research-public/tests/test_public_bundle.py
    python3 -m pytest -q site/momentum-research-public/tests/test_public_bundle.py

验证内容包括严格字段、六策略当前快照、决策类型、Top3/Top5 候选、11M 收益字段、
双轨模型、禁止隐私键、绝对路径、外部页面资源，以及 Pages artifact 的显式文件
allowlist。

在上游研究仓库中重新生成公开数据：

    python3 scripts/build_public_momentum_site_data.py

生成器从空字典开始，只读取聚合研究结果；不会复制私人输出目录。

## GitHub Pages 模板

.github-workflow-template/pages.yml 是审阅模板，不会自动生效。确认公开数据和
分支保护后，才把它复制到仓库根部的 .github/workflows/pages.yml。

模板采用手动触发、最小权限和临时 staging 目录；它逐个复制以下公开文件，
绝不上传整个仓库或任何生成目录：

- index.html
- styles.css
- app.js
- public-study.schema.json
- data/study.json

所有页面资源均使用相对路径，因此可安全部署到 GitHub Pages 的仓库子路径。

## 公开数据生成器契约

生成器必须：

1. 创建全新字典，而不是过滤私人 artifact。
2. 分别写入 existing-sleeve 与 new-capital 模型轨。
3. 将 Top3 固定为 SNDK / MU / WDC，Top5 再加入 LITE / INTC。
4. 质量状态只使用 PASS、WATCH、BLOCK_DECISION_GRADE。
5. 候选数值只写可验证的 11M momentumReturn，不写主观质量分。
6. 决策记录写 targetExposure，不写主观 confidence；无法历史回放的市场门应在
   reason 中明确写 historical market gate unavailable。
7. 生成后先运行本目录测试，再允许人工触发 Pages workflow。
8. 为六个策略写入 `currentSnapshot`，并为所有历史决策写入 `kind`；页面不得从
   自由文本猜测生产持有与新资金门禁的关系。
