import csv, re
from collections import defaultdict

path="/Users/linji/Downloads/History_for_Account_Z20695967.csv"
rows=[]
with open(path) as f:
    for row in csv.reader(f):
        if len(row)<13 or row[0]=="Run Date" or not re.match(r"\d{2}/\d{2}/\d{4}",row[0]): continue
        rows.append(row)
def fnum(s):
    s=s.strip().replace(",","")
    try: return float(s)
    except: return 0.0
def pdate(s):
    m,d,y=s.split("/"); return (int(y),int(m),int(d))

final={"NVDA","VOO","QQQ","MRVL","DRAM","ARM","GOOGL","AMD","AMZN","SPMO","AAPL",
"TSM","SPY","ASX","TQQQ","DELL","ASML","AVGO","LRCX","INTC","NOK","MU","LITE"}

buy_q=defaultdict(float); sell_q=defaultdict(float)
buy_d=defaultdict(float); sell_d=defaultdict(float)
opt_buy_d=defaultdict(float); opt_sell_d=defaultdict(float)
month_buy=defaultdict(float); month_sell=defaultdict(float)
tot_buy=tot_sell=0.0
for row in rows:
    date,action,sym,qty,amt=row[0],row[1],row[2].strip(),fnum(row[6]),fnum(row[10])
    if "Electronic Funds" in action or "TRANSFERRED" in action or sym=="": continue
    is_opt= sym.startswith("-") or "CALL" in action or "PUT" in action
    mon=date[:2]
    if amt<0:  # bought
        tot_buy+=-amt; month_buy[mon]+=-amt
        if not is_opt: buy_q[sym]+=qty; buy_d[sym]+=-amt
        else: opt_buy_d[sym]+=-amt
    else:
        tot_sell+=amt; month_sell[mon]+=amt
        if not is_opt: sell_q[sym]+=-qty; sell_d[sym]+=amt
        else: opt_sell_d[sym]+=amt

# 期间完全清仓 / 波段股（出现在卖出, 且不在最终持仓）
exited=sorted([s for s in set(list(buy_q)+list(sell_q)) if s not in final])
print("=== 期间被卖出且当前已不持有（已清仓 / 旧底仓了结）===")
print(f"{'股票':6}{'买入股数':>10}{'卖出股数':>10}{'买入$':>10}{'卖出$':>10}")
for s in exited:
    if sell_q[s]>0 or buy_q[s]>0:
        print(f"{s:6}{round(buy_q[s],2):>10}{round(sell_q[s],2):>10}{buy_d[s]:>10.0f}{sell_d[s]:>10.0f}")

print("\n=== 期权交易（现金流）===")
for s in sorted(set(list(opt_buy_d)+list(opt_sell_d))):
    print(f"  {s:20} 买入${opt_buy_d[s]:.0f}  卖出${opt_sell_d[s]:.0f}  净${opt_sell_d[s]-opt_buy_d[s]:+.0f}")

print("\n=== 分月资金活动 ===")
for m in sorted(month_buy):
    print(f"  {m}月: 买入 ${month_buy[m]:,.0f}   卖出 ${month_sell[m]:,.0f}   净投入 ${month_buy[m]-month_sell[m]:,.0f}")
print(f"\n  合计: 买入 ${tot_buy:,.0f}   卖出 ${tot_sell:,.0f}   净买入 ${tot_buy-tot_sell:,.0f}")
