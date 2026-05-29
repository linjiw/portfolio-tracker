import csv, re, json
from collections import defaultdict, OrderedDict

HIST="/Users/linji/Downloads/History_for_Account_Z20695967.csv"
PORT="/Users/linji/Downloads/Portfolio_Positions_May-28-2026 (1).csv"
prices=json.load(open("/tmp/prices.json"))

def fnum(s):
    s=str(s).strip().replace(",","").replace("$","").replace("+","")
    if s in ("","--"): return 0.0
    try: return float(s)
    except: return 0.0
def iso(d):
    m,dd,y=d.split("/"); return f"{y}-{int(m):02d}-{int(dd):02d}"

# ---- parse history ----
hrows=[]
with open(HIST) as f:
    for r in csv.reader(f):
        if len(r)<13 or r[0]=="Run Date" or not re.match(r"\d{2}/\d{2}/\d{4}",r[0]): continue
        hrows.append(r)

txns=defaultdict(list)        # sym -> list
opt_txns=defaultdict(list)
deposits=0.0; tot_buy=tot_sell=0.0
names={}
for r in hrows:
    date,action,sym,desc,price,qty,amt = r[0],r[1],r[2],r[3],r[5],r[6],r[10]
    sym=sym.strip()
    if "Electronic Funds" in action or "TRANSFERRED" in action:
        deposits+=fnum(amt); continue
    if sym=="" : continue
    is_opt = sym.startswith("-") or "CALL" in action or "PUT" in action
    side = "BUY" if "BOUGHT" in action else ("SELL" if "SOLD" in action else "?")
    q=abs(fnum(qty)); p=fnum(price); a=fnum(amt)
    rec={"date":iso(date),"side":side,"qty":q,"price":p,"amount":a}
    if is_opt:
        opt_txns[sym].append(rec)
    else:
        txns[sym].append(rec)
        names.setdefault(sym, desc.strip())
        if a<0: tot_buy+=-a
        else: tot_sell+=a

# ---- parse portfolio (current actual) ----
cur={}  # sym -> {shares, price, value, gain, gainpct, cost, avg}
with open(PORT) as f:
    for r in csv.reader(f):
        if len(r)<14 or r[0]=="Account Number" or not r[0].startswith("Z"): continue
        sym=r[2].strip()
        if sym in ("SPAXX**","Pending activity","") or sym.startswith("-"): continue
        shares=fnum(r[4]); price=fnum(r[5]); val=fnum(r[7])
        tg=fnum(r[10]); cost=fnum(r[13])
        if sym not in cur:
            cur[sym]={"shares":0.0,"price":price,"value":0.0,"gain":0.0,"cost":0.0}
        cur[sym]["shares"]+=shares; cur[sym]["value"]+=val
        cur[sym]["gain"]+=tg; cur[sym]["cost"]+=cost; cur[sym]["price"]=price
for s,d in cur.items():
    d["avg"]= d["cost"]/d["shares"] if d["shares"] else 0
    d["gainpct"]= d["gain"]/d["cost"]*100 if d["cost"] else 0

# ---- helpers ----
def price_on(sym, isodate):
    ps=prices.get(sym,{})
    if not ps: return None
    if isodate in ps: return ps[isodate]
    keys=sorted(ps)
    prev=None
    for k in keys:
        if k<=isodate: prev=ps[k]
        else: break
    return prev if prev is not None else ps[keys[0]]

# ---- per-stock engine ----
allsyms=sorted(set(list(txns)+list(cur)))
stocks=[]
total_realized=0.0
for sym in allsyms:
    tl=sorted(txns.get(sym,[]), key=lambda x:(x["date"]))
    net=sum((t["qty"] if t["side"]=="BUY" else -t["qty"]) for t in tl)
    final=cur.get(sym,{}).get("shares",0.0)
    open_qty=round(final-net,4)
    held = final>0.0001
    # est opening price = real historical price at first txn date (or 3/30)
    first_date = tl[0]["date"] if tl else "2026-03-30"
    est_open = price_on(sym, first_date) or (tl[0]["price"] if tl else 0)
    has_legacy = open_qty>0.001
    qty = open_qty if open_qty>0 else 0.0
    cost = qty*est_open
    realized=0.0
    rows=[]
    if has_legacy:
        rows.append({"date":"≤2026-03-30","side":"OPEN","qty":round(open_qty,3),
                     "price":round(est_open,2),"amount":round(-open_qty*est_open,2),
                     "pos":round(qty,3),"avg":round(est_open,2),"realized":None,"legacy":True})
    for t in tl:
        if t["side"]=="BUY":
            qty+=t["qty"]; cost+=abs(t["amount"]); r=None
        else:
            avg = cost/qty if qty>1e-9 else t["price"]
            r=(t["price"]-avg)*t["qty"]; realized+=r
            cost=max(0.0,cost-avg*t["qty"]); qty=max(0.0,qty-t["qty"])
        avgnow = cost/qty if qty>1e-9 else 0
        rows.append({"date":t["date"],"side":t["side"],"qty":round(t["qty"],3),
                     "price":round(t["price"],2),"amount":round(t["amount"],2),
                     "pos":round(qty,3),"avg":round(avgnow,2),
                     "realized":(round(r,2) if r is not None else None),"legacy":False})
    total_realized+=realized
    c=cur.get(sym,{})
    pser=sorted(prices.get(sym,{}).items())
    stocks.append({
        "sym":sym,"name":names.get(sym,sym),"held":held,
        "shares":round(final,3),"avg":round(c.get("avg",0),2),
        "curPrice":round(c.get("price",0) or (price_on(sym,"2026-05-28") or 0),2),
        "value":round(c.get("value",0),2),"unreal":round(c.get("gain",0),2),
        "unrealPct":round(c.get("gainpct",0),2),"cost":round(c.get("cost",0),2),
        "realized":round(realized,2),"hasLegacy":has_legacy,
        "numTrades":len(tl),"prices":pser,"txns":rows
    })

# ---- options summary ----
opts=[]
opt_net=0.0
for sym in sorted(opt_txns):
    tl=sorted(opt_txns[sym],key=lambda x:x["date"])
    net=sum(t["amount"] for t in tl); opt_net+=net
    opts.append({"sym":sym,"net":round(net,2),
        "txns":[{"date":t["date"],"side":t["side"],"qty":t["qty"],"price":t["price"],"amount":round(t["amount"],2)} for t in tl]})

held_val=sum(s["value"] for s in stocks if s["held"])
held_unreal=sum(s["unreal"] for s in stocks if s["held"])
summary={
    "marketValue":round(held_val,2),
    "unrealized":round(held_unreal,2),
    "realized":round(total_realized,2),
    "netInvested":round(tot_buy-tot_sell,2),
    "totalBuy":round(tot_buy,2),"totalSell":round(tot_sell,2),
    "deposits":round(deposits,2),"optNet":round(opt_net,2),
    "dateRange":["2026-03-30","2026-05-28"],
    "numStocks":len(stocks),"numHeld":sum(1 for s in stocks if s["held"])
}
payload={"summary":summary,"stocks":stocks,"options":opts}
json.dump(payload, open("/tmp/payload.json","w"))
print("stocks:",len(stocks),"held:",summary["numHeld"],"options:",len(opts))
print("realized(window,est):",summary["realized"]," unrealized:",summary["unrealized"]," mktVal:",summary["marketValue"])
