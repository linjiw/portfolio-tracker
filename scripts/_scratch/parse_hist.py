import csv, re
from collections import defaultdict

path = "/Users/linji/Downloads/History_for_Account_Z20695967.csv"
rows=[]
with open(path) as f:
    r=csv.reader(f)
    for row in r:
        if len(row)<13: continue
        if row[0]=="Run Date": continue
        if not re.match(r"\d{2}/\d{2}/\d{4}", row[0]): continue
        rows.append(row)

# columns: 0 RunDate,1 Action,2 Symbol,3 Desc,4 Type,5 Price,6 Qty,7 Comm,8 Fees,9 AccrInt,10 Amount,11 Cash,12 Settle
def fnum(s):
    s=s.strip().replace(",","")
    if s=="" : return 0.0
    try: return float(s)
    except: return 0.0

# parse date to sortable
def pdate(s):
    m,d,y=s.split("/")
    return (int(y),int(m),int(d))

trades=[]   # (date, sym, qty, amount, action, is_option)
buys=defaultdict(float); sells=defaultdict(float)
buy_amt=defaultdict(float); sell_amt=defaultdict(float)
realized_proceeds=0.0
for row in rows:
    date=row[0]; action=row[1]; sym=row[2].strip(); qty=fnum(row[6]); amt=fnum(row[10])
    if "Electronic Funds" in action or "TRANSFERRED" in action or sym=="" or sym==" ":
        continue
    is_opt = sym.startswith("-") or "CALL" in action or "PUT" in action
    trades.append((pdate(date),date,sym,qty,amt,action,is_opt))

trades.sort(key=lambda x:x[0])

# Build cumulative positions over time (shares only, non-option) 
pos=defaultdict(float)
opt_pos=defaultdict(float)
# snapshots at end of selected dates
checkpoints=["03/30/2026","04/17/2026","04/30/2026","05/08/2026","05/15/2026","05/22/2026","05/28/2026"]
cp_set=set(pdate(c) for c in checkpoints)
snapshots={}

# group trades by date in order
from itertools import groupby
all_dates=sorted(set(t[0] for t in trades))
ti=0
for d in all_dates:
    for t in trades:
        if t[0]==d:
            pd_,date,sym,qty,amt,action,is_opt=t
            if is_opt:
                opt_pos[sym]+=qty
            else:
                pos[sym]+=qty
    if d in cp_set:
        snap={k:round(v,3) for k,v in pos.items() if abs(v)>0.0001}
        snapshots[date]= dict(sorted(snap.items()))

# print snapshots
for c in checkpoints:
    # find matching date label
    key=None
    for date in snapshots:
        if pdate(date)==pdate(c): key=date
    if key:
        print(f"\n===== 截至 {key} 的持仓（股/份额） =====")
        tot=len(snapshots[key])
        for sym,q in snapshots[key].items():
            print(f"  {sym:8} {q}")
        print(f"  ({tot} 个持仓)")
