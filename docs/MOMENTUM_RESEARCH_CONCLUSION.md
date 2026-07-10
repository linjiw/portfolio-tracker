# 动量辅助策略研究结论

数据截止：2026-07-10。本文是研究记录，不是投资建议或自动下单指令。

## 最终决定

- 正式策略：`KEEP_BASELINE`。继续使用无杠杆 SPMO sleeve，并用已有市场门、相对强弱、reclaim 入场和生命周期移动保护管理账户风险。
- 最好的可检验假设：`SPMO_10M_ON125_OFF50`，仅 `ALLOW_SHADOW`，不得据此升级实盘。
- RL：`INCONCLUSIVE_RESEARCH / FAIL_TO_QUALIFY`。
- Top-3：`BLOCK_DATA`；未取得 point-in-time 成分、退市回报和永久证券标识前，收益结果不得用于决策或 RL。

研究目标是净收益不低于基准且最大回撤至少缩小 15%。没有任何普通 overlay 或 RL challenger 在规定的时序、横截面和 replay 门槛上完成这一目标，因此不因回测看起来更平滑而替换基准。

## 关键证据

修复指标 warm-up、部分仓位自融资漂移、Top-3 初始持仓继承和止损优先级后，SPMO 10 个月非对称候选的描述统计为：

| 区间 | 10m ON=1.25 / OFF=0.50 | SPMO buy-and-hold | 结果 |
|---|---:|---:|---|
| 2016-07-11–2021-12-31 train | 21.68% CAGR / -20.94% MDD | 19.44% / -30.95% | 通过 |
| 2022–2023 validation | 1.45% / -19.96% | 2.61% / -22.74% | 收益失败 |
| 2024+ fixed replay | 42.57% / -17.25% | 41.09% / -20.13% | 同时改善，但回撤仅改善约 14.3% |
| 完整十年 | 22.15% / -20.94% | 20.86% / -30.95% | 描述性同时改善 |

把同一候选原样用于 SPY、QQQ、PDP、MTUM、MMTM、SPMO、QMOM 后，它在开发期排名 21/27，五个直接网格邻居没有一个开发分为正；2024+ 仅 3/7 同时改善收益和回撤，严格门槛 0/7，中位 CAGR 落后约 0.41 个百分点。年度 rolling pseudo-OOS 的动态选择也为 0/7。20bp/边、T-bill+6% 压力下，SPMO replay 的收益优势已经很薄；50bp 压力会翻转。

固定止盈没有稳定价值；固定止损只在部分趋势状态有帮助。最可靠的行为是让右尾赢家运行，用趋势确认、仓位上限和只上移的保护线控制风险，而不是反复微调止盈点。

## 我最喜欢的正式执行规则

这是一套小仓位、非预测式的 SPMO 生命周期策略：

1. 以 8% 账户权重为基础 sleeve，12% 为硬上限，不用 ETF 层面的常态杠杆。
2. 新增仓必须同时满足市场 sentinel 明确 `ALLOW`、SPMO 高于 EMA50、没有连续两日跌破 EMA21、相对 SPY 的 21/63 日强弱为正，且没有硬跌或过度延伸。
3. 只用 reclaim/buy-stop 或回踩确认分三批进入；不追市价。
4. 不设固定止盈。已有赢家使用生命周期 3ATR moving stop，只能上移；EMA50 失守触发 defend/trim 复核。
5. 条件不满足时保持现金或原有合规仓位；`WATCH/BLOCK_DATA` 一律禁止新加仓。

研究 challenger 可在影子账本中把 8% 基础袖套映射为：完成月 10 个月趋势 ON 时目标 10%（1.25×），OFF 时目标 4%（0.50×）；信号在月末收盘确认，下一交易日收盘成交。它必须冻结，不再寻找 8/9/11 个月的更漂亮参数。

## 当前状态

截至 2026-07-10：SPMO 153.75，EMA21 153.05，EMA50 147.41，ATR14 4.82。公开研究只给出 8% 模型袖套目标，不披露账户实仓、股数或成本；当前新加仓为 `BLOCK`，既有模型袖套为 `HOLD_WITH_MOVING_STOP`。

- buy-stop/reclaim：154.23
- stop-limit ceiling：154.71
- EMA50 invalidation close：146.44
- 已保存生命周期 3ATR moving stop：139.98，只能上移
- 当前最大新增仓：0%

## RL 环境结论

环境提供 0/25/50/75/100/125% 目标、真实 `HOLD` 和动态连续 `BASELINE`。普通基准使用相同周/月调仓日、连续目标、仓位漂移、T-bill 现金、融资和 next-close 成交；逐日 equity/exposure/turnover 与普通引擎匹配到浮点精度。状态包含当前与历史最大回撤；支持按唯一日期计数，缺乏候选或基准支持时强制回退。

3-epoch、8 组配置、3 个种子、7 资产回放的严格通过为 0/7，当前七资产动作全部回退 `BASELINE`。该原型仍缺年度 nested RL、同步 block bootstrap、2x 成本/延迟压力、DSR/PBO 和新的 prospective 数据，因此只能保留为研究框架。

## 数据边界与下一步

ETF 回测使用 yfinance/Yahoo 的 auto-adjusted market-price return proxy，不是发行人官方 NAV total return；`actions=False` 没有保存可独立审计的企业行动账本。调整后 OHLC 不能证明盘中 buy-stop 或 stop-loss 的真实成交。

2024+ 已被此前研究查看，只能称为 fixed replay。真正升级 challenger 需要从 2026-07-13 起冻结代码、参数和哈希，至少观察 24 个月或一个完整 `ON→OFF→ON` 周期，并同时满足净 CAGR 不低于基准、MDD 不高于基准的 85%，且计入实际滑点和融资成本。

完整结果见：

- `output/momentum_overlay_research/momentum_overlay_research.md`
- `output/momentum_policy_lab/momentum_policy_lab.md`
- `output/momentum_final_candidate/momentum_final_candidate.md`
- `output/momentum_rl_research/momentum_rl_research.md`

主要研究依据：

- [S&P 500 Momentum Index 与发布前回测披露](https://www.spglobal.com/spdji/en/indices/dividends-factors/sp-500-momentum-index/)
- [S&P Momentum Indices Methodology](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-momentum-indices.pdf)
- [Volatility Managed Portfolios](https://www.nber.org/papers/w22208)
- [When do stop-loss rules stop losses?](https://www.sciencedirect.com/science/article/abs/pii/S138641811300030X)
- [Safe Policy Improvement with Baseline Bootstrapping](https://proceedings.mlr.press/v97/laroche19a.html)
- [Nasdaq Global Index Watch](https://www.nasdaq.com/solutions/global-indexes/data/giw) 与 [CRSP 数据指南](https://www.crsp.org/wp-content/uploads/guides/CRSP_US_Stock_%26_Indexes_Database_Guide_Flat_File_Format_2.0.pdf)
