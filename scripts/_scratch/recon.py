import csv, re
from collections import defaultdict

path = "/Users/linji/Downloads/History_for_Account_Z20695967.csv"
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

# Final actual holdings (non-option) from Portfolio CSV May 28
final={"NVDA":120.109,"VOO":29.858,"QQQ":18.760,"MRVL":12,"DRAM":36,"ARM":2,
"GOOGL":42.25,"AMD":2.2,"AMZN":12.683,"SPMO":36.059,"AAPL":8.376,"TSM":5.9,
"SPY":2.7,"ASX":6,"TQQQ":3,"DELL":0.5,"ASML":0.65,"AVGO":0.4,"LRCX":1,
"INTC":4,"NOK":20,"MU":6.538,"LITE":1.3}

# net qty change per (symbol) per date (non-option only)
trades=[]
deposits=0.0
for row in rows:
    date,action,sym,qty,amt=row[0],row[1],row[2].strip(),fnum(row[6]),fnum(row[10])
    if "Electronic Funds" in action or "TRANSFERRED" in action:
        deposits+=amt; continue
    if sym in ("",) : continue
    is_opt = sym.startswith("-") or "CALL" in action or "PUT" in action
    trades.append((pdate(date),date,sym,qty,amt,is_opt))
trades.sort(key=lambda x:x[0])

checkpoints=["03/30/2026","04/17/2026","04/30/2026","05/08/2026","05/15/2026","05/22/2026","05/28/2026"]
labels={"03/30/2026":"3月底(期初)","04/17/2026":"4月中","04/30/2026":"4月底",
"05/08/2026":"5月初","05/15/2026":"5月中","05/22/2026":"5月下旬","05/28/2026":"5/28(期末)"}

# net change after a given checkpoint date (strictly after)
def holding_at(sym, cp):
    cpd=pdate(cp)
    net_after=sum(t[3] for t in trades if (not t[5]) and t[2]==sym and t[0]>cpd)
    return final.get(sym,0)-net_after

print("反向推算：当前持有股票在各时间点的份额\n")
syms=sorted(final.keys())
hdr="股票  "+ "".join(f"{labels[c]:>11}" for c in checkpoints)
print(hdr)
for sym in syms:
    line=f"{sym:6}"
    for c in checkpoints:
        v=holding_at(sym,c)
        line+=f"{round(v,2):>11}"
    print(line)

print(f"\n期间累计现金转入(Electronic Funds + Transfer): ${deposits:,.0f}")
