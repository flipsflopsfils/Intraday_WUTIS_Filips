import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

# Downloads 1-minute SPY bars (SIP feed, raw prices) and cash dividends from Alpaca.
# Needs ALPACA_API_KEY and ALPACA_SECRET_KEY in a .env file (see .env.example).

START, END = "2019-10-07", "2026-10-06"
URL = "https://data.alpaca.markets"

load_dotenv()
HEADERS = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
           "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def get_all_pages(url, params):
    pages = []
    while True:
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
        r.raise_for_status()
        pages.append(r.json())
        token = pages[-1].get("next_page_token")
        if not token:
            return pages
        params["page_token"] = token
        time.sleep(0.3)   # pause for some small time before requesting the next page


def download_bars():
    params = {"timeframe": "1Min", "start": START + "T00:00:00-04:00", "end": END + "T23:59:59-04:00",
              "feed": "sip", "adjustment": "raw", "limit": 10000, "sort": "asc"}
    pages = get_all_pages(URL + "/v2/stocks/SPY/bars", params)
    bars = pd.DataFrame([bar for page in pages for bar in page["bars"]])
    bars = bars.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    bars.index = pd.to_datetime(bars.pop("timestamp"), utc=True).dt.tz_convert("America/New_York")
    bars = bars[["open", "high", "low", "close", "volume"]].between_time("09:30", "15:59")
    return bars


def download_dividends():
    params = {"symbols": "SPY", "start": START, "end": "2027-01-04", "limit": 1000}
    pages = get_all_pages(URL + "/v1/corporate-actions", params)
    rows = [d for page in pages for d in page["corporate_actions"].get("cash_dividends", [])]
    div = pd.DataFrame({"ex_dividend_date": [d["ex_date"] for d in rows], "cash_amount": [d["rate"] for d in rows]})
    return div[div.ex_dividend_date.between(START, END)].sort_values("ex_dividend_date")


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    bars = download_bars()
    bars.to_csv("data/spy_1min.csv", index_label="timestamp", float_format="%.12g")
    print(len(bars), "minute bars,", bars.index.normalize().nunique(), "trading days")

    # quick check for missing minutes, e.g. the trading halts in March 2020 mentined in the README
    counts = bars.groupby(bars.index.date).size()
    print("days with less than 390 bars:")
    print(counts[counts < 390])

    div = download_dividends()
    div.to_csv("data/spy_dividends.csv", index=False)
    print(len(div), "dividends")
