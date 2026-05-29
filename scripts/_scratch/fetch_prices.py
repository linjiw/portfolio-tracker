import yfinance as yf, json, warnings
warnings.filterwarnings('ignore')

tickers = ["NVDA","VOO","QQQ","MRVL","DRAM","ARM","GOOGL","AMD","AMZN","SPMO","AAPL",
"TSM","SPY","ASX","TQQQ","DELL","ASML","AVGO","LRCX","INTC","NOK","MU","LITE",
"ADBE","ASTS","CBRS","CRWV","GLD","HOOD","IREN","ISRG","LLY","MCD","META","MRK",
"MSFT","NBIS","NVO","ORCL","PFE","QCOM","RKLB","SNDK","TSLA","XPEV","XIACY","MSTR"]

data = yf.download(tickers, start="2026-03-25", end="2026-05-29",
                   progress=False, auto_adjust=True, group_by="ticker")
out={}
for t in tickers:
    try:
        sub = data[t]["Close"].dropna()
        series = {d.strftime("%Y-%m-%d"): round(float(v),2) for d,v in sub.items()}
        if series: out[t]=series
    except Exception as e:
        pass
print("fetched tickers:", len(out), "/", len(tickers))
missing=[t for t in tickers if t not in out]
print("missing:", missing)
json.dump(out, open("/tmp/prices.json","w"))
