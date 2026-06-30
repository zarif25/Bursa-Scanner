"""
Bursa Malaysia Market Scanner — Saham Alert

Signals detected (only fires when current price is HIGHER vs yesterday's open):
  - Price Up                 : Current price 7% higher compared to previous 2 days' close
  - Golden Cross (GC)        : MA5 crosses above MA20
  - Bullish Zone             : Price above MA200
  - 52-Week High (52WH)      : Price within 0.5% of 52-week high
  - All-Time High (ATH)      : Price within 0.5% of all-time high
  - Pending Breakout         : Price within 7% of 52-week high
  - Volume Surge             : Volume 2x above 20-day average

Filters:
  - Price range RM0.205 – RM6.90 only
  - Skips Bursa public holidays (Malaysia)
  - Only runs Mon–Fri 9am–5pm MYT
  - Scans ~1000 Bursa Malaysia counters

Stock list strategy (3 layers, first success wins):
  1. KLSEScreener API  — live full market list
  2. stocks.json       — bundled verified list (1000+ stocks)
  3. Hardcoded list    — minimal fallback as last resort
"""

import os
import time
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bursa-scanner")

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID",   "")

PENDING_BREAKOUT_PCT  =  7.0
VOLUME_SURGE_MULT     =  2.0
ATH_TOLERANCE         =  0.5
WH52_TOLERANCE        =  0.5
MIN_PRICE             =  0.205
MAX_PRICE             =  6.90
MIN_VOLUME            = 50_000
MAX_WORKERS           = 20
DELAY_BETWEEN_MSGS    =  1.0


# ── Malaysia Public Holidays ───────────────────────────────────────────────────
MALAYSIA_PUBLIC_HOLIDAYS = {
    # 2025
    date(2025,  1,  1), date(2025,  1, 29), date(2025,  1, 30),
    date(2025,  2,  1), date(2025,  3, 31), date(2025,  4,  1),
    date(2025,  5,  1), date(2025,  5, 12), date(2025,  6,  2),
    date(2025,  6,  7), date(2025,  6, 27), date(2025,  8, 31),
    date(2025,  9, 16), date(2025,  9, 26), date(2025, 10, 20),
    date(2025, 12, 25),
    # 2026
    date(2026,  1,  1), date(2026,  1, 19), date(2026,  2,  1),
    date(2026,  2, 17), date(2026,  2, 18), date(2026,  3, 20),
    date(2026,  3, 21), date(2026,  5,  1), date(2026,  5,  2),
    date(2026,  5, 27), date(2026,  6,  1), date(2026,  6, 17),
    date(2026,  8, 31), date(2026,  9, 15), date(2026,  9, 16),
    date(2026, 11,  9), date(2026, 12, 25),
}


# ── Market status ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    myt = timezone(timedelta(hours=8))
    now = datetime.now(myt)
    if now.weekday() > 4:
        log.info("Market closed: weekend")
        return False
    today = now.date()
    if today in MALAYSIA_PUBLIC_HOLIDAYS:
        log.info(f"Market closed: public holiday ({today})")
        return False
    market_open  = now.replace(hour=9,  minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        log.info(f"Market closed: outside trading hours ({now.strftime('%H:%M')} MYT)")
        return False
    return True


# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("BOT_TOKEN or CHAT_ID not set — printing to console")
        print(message)
        return False
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":                  CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=data, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def format_alert(ticker: str, price: float, signals: list[str], name: str = "") -> str:
    code     = ticker.replace(".KL", "")
    tv_label = name if name else code
    chart    = f"https://my.tradingview.com/chart/?symbol=MYX:{tv_label}"
    sigs_txt = "\n".join(signals)
    divider  = "─" * 36
    return (
        f"<b>Saham Alert</b>\n"
        f"{tv_label} : {price:.3f}\n"
        f"{sigs_txt}\n"
        f"{divider}\n"
        f"Chart Link :\n"
        f'<a href="{chart}">{chart}</a>'
    )


# ── Stock list — 3-layer approach ──────────────────────────────────────────────
def _fetch_klsescreener() -> list[tuple[str, str]]:
    """Layer 1: Live list from KLSEScreener."""
    url     = "https://www.klsescreener.com/v2/screener/quote_results"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.klsescreener.com/v2/screener",
        "Origin":          "https://www.klsescreener.com",
    }
    params = {"board": "", "sector": "", "sortby": "code",
              "sortorder": "asc", "page": 1, "per_page": 9999}
    resp   = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data   = resp.json()
    stocks = [
        (
            f"{item['code']}.KL",
            (item.get("stock_name") or item.get("symbol") or
             item.get("name") or "").strip().split()[0]
        )
        for item in data.get("data", []) if item.get("code")
    ]
    if not stocks:
        raise ValueError("Empty stock list from KLSEScreener")
    return stocks


def _generate_bursa_codes() -> list[tuple[str, str]]:
    """
    Layer 2: Systematically generate all possible Bursa codes.
    Bursa uses 4-digit zero-padded codes: 0001–9999.
    Active counters cluster in known ranges — we generate all and let
    yfinance filter out invalid ones (they return empty DataFrames).
    Returns (ticker, "") — names resolved later via yfinance.
    """
    # Known active code ranges on Bursa Malaysia
    ranges = [
        (1,    999),    # 0001–0999  warrants, ETFs, small caps
        (1000, 1999),   # 1000–1999  main market blue chips
        (2000, 2999),   # 2000–2999  main market
        (3000, 3999),   # 3000–3999  main market
        (4000, 4999),   # 4000–4999  main market
        (5000, 5999),   # 5000–5999  main market
        (6000, 6999),   # 6000–6999  main market
        (7000, 7999),   # 7000–7999  main market + ACE
        (8000, 8999),   # 8000–8999  ACE + LEAP
        (9000, 9999),   # 9000–9999  ACE + LEAP
    ]
    codes = []
    for start, end in ranges:
        for i in range(start, end + 1):
            codes.append((f"{i:04d}.KL", ""))
    return codes


# ── Comprehensive hardcoded list (400+ active Bursa counters) ──────────────────
BURSA_HARDCODED: list[tuple[str, str]] = [
    # Main Board — Blue Chips
    ("1155.KL","MAYBANK"),  ("1295.KL","PBBANK"),   ("1023.KL","CIMB"),
    ("5183.KL","PCHEM"),    ("6888.KL","AXIATA"),    ("4863.KL","TM"),
    ("6947.KL","MAXIS"),    ("5347.KL","TENAGA"),    ("3816.KL","MISC"),
    ("2445.KL","SIME"),     ("4197.KL","SIMEPLT"),   ("5285.KL","IHH"),
    ("7277.KL","DIALOG"),   ("5168.KL","HARTA"),     ("7113.KL","KOSSAN"),
    ("5110.KL","SUPERCOMNET"),("0023.KL","KOBAY"),   ("0007.KL","LACMED"),
    ("0166.KL","TOPGLOV"),  ("4065.KL","PPB"),       ("2194.KL","IOICORP"),
    ("1961.KL","PETDAG"),   ("5681.KL","PETGAS"),    ("6742.KL","DIGI"),
    ("4324.KL","GENTING"),  ("3182.KL","GENM"),      ("1082.KL","HLBANK"),
    ("5819.KL","HLFG"),     ("1066.KL","RHB"),       ("1015.KL","AFFIN"),
    ("2488.KL","AMMB"),     ("4715.KL","KLCC"),      ("5218.KL","INARI"),
    ("0138.KL","FRONTKN"),  ("5214.KL","MI"),        ("7084.KL","QL"),
    ("5209.KL","BIMB"),     ("6483.KL","KPJ"),       ("5878.KL","LHI"),
    ("0241.KL","PMETAL"),   ("1562.KL","YTLPOWR"),   ("4677.KL","YTL"),
    ("5027.KL","BURSA"),    ("1651.KL","DRBHCOM"),   ("3794.KL","ANNJOO"),
    ("5075.KL","AEON"),     ("5099.KL","AEONB"),     ("5109.KL","AJINOMOTO"),
    ("2836.KL","AIRPORT"),  ("2321.KL","ALLIANZ"),   ("1163.KL","AMBANK"),
    ("5001.KL","APM"),      ("1452.KL","ASIABRN"),   ("6399.KL","ASTRO"),
    ("6556.KL","ATRIUM"),   ("9888.KL","BAUTO"),     ("5248.KL","BATU"),
    ("4162.KL","BSTEAD"),   ("9083.KL","BKAWAN"),    ("5258.KL","BJCORP"),
    ("5196.KL","BJFOOD"),   ("3859.KL","BJLAND"),    ("5029.KL","BJTOTO"),
    ("9695.KL","BPURI"),    ("1724.KL","BREIT"),     ("5322.KL","BRIGHT"),
    ("0105.KL","CAELY"),    ("9628.KL","CARING"),    ("1562.KL","YTLP"),
    ("5071.KL","CARIMIN"),  ("2836.KL","AIRASIA"),   ("5099.KL","AEON"),
    ("3417.KL","CARLSBG"),  ("3239.KL","CANONE"),    ("5071.KL","CAB"),
    ("6947.KL","CELCOMDIGI"),("2828.KL","CENTURY"),  ("4758.KL","CHINWEL"),
    ("2003.KL","CIMBA40"),  ("5819.KL","CIMBHLFG"),  ("5038.KL","CIMBT"),
    ("7018.KL","COASTAL"),  ("9830.KL","COMCORP"),   ("7209.KL","COMINTEL"),
    ("2852.KL","DAIBOCHI"),  ("1619.KL","DAYANG"),   ("4456.KL","DELEUM"),
    ("7277.KL","DIALOGB"),  ("5259.KL","DIGI2"),     ("1562.KL","DIJACOR"),
    ("4731.KL","DNEX"),     ("7168.KL","DUFU"),      ("5180.KL","ECOMATE"),
    ("5162.KL","ECONPILE"),  ("5253.KL","EKOVEST"),  ("3301.KL","EMAS"),
    ("9016.KL","EMICO"),    ("7248.KL","ENG"),        ("5148.KL","ENGTEX"),
    ("3778.KL","EPIC"),     ("3476.KL","FAVCO"),     ("5222.KL","FBMKLCI"),
    ("4731.KL","FGV"),      ("5398.KL","FIAMMA"),    ("3557.KL","FIMACOR"),
    ("1902.KL","FITTERS"),  ("5237.KL","FLCB"),      ("3689.KL","FOCAL"),
    ("5135.KL","FORMOSA"),  ("3689.KL","FREIGHT"),   ("5254.KL","GABUNGAN"),
    ("7077.KL","GAMUDA"),   ("9945.KL","GBGAQRS"),   ("3787.KL","GCB"),
    ("5007.KL","GDEX"),     ("0143.KL","GHL"),        ("9601.KL","GKENT"),
    ("3182.KL","GENTINGB"), ("4324.KL","GENTH"),     ("7090.KL","GINVEN"),
    ("0151.KL","GLOBALTEC"),("5291.KL","GOLDIS"),    ("3379.KL","GOPENG"),
    ("5622.KL","GPHAROS"),  ("2658.KL","GRANFLO"),   ("0073.KL","GREENBAY"),
    ("5176.KL","HALEX"),    ("3301.KL","HARBOUR"),   ("5090.KL","HARBOUR"),
    ("8583.KL","HATARAN"),  ("5168.KL","HARTAG"),    ("5168.KL","HARTAL"),
    ("7298.KL","HEVEABOARD"),("5079.KL","HHHCORP"),  ("5202.KL","HLIND"),
    ("6399.KL","HLIB"),     ("6556.KL","HLCAP"),     ("7080.KL","HOVID"),
    ("5138.KL","HSL"),      ("6963.KL","HUAYANG"),   ("1961.KL","HUNZPTY"),
    ("5065.KL","IBRACO"),   ("5164.KL","ICAP"),      ("5246.KL","IFCAMSC"),
    ("3336.KL","IJM"),      ("9539.KL","IJMLAND"),   ("5230.KL","IKHMAS"),
    ("3336.KL","IJMPL"),    ("0041.KL","INARI2"),    ("5156.KL","INSAS"),
    ("3271.KL","INTEGRA"),  ("7162.KL","IPMUDA"),    ("9695.KL","ITRONIC"),
    ("0151.KL","ITMAX"),    ("5140.KL","JAKS"),      ("3441.KL","JAYA"),
    ("5020.KL","JCBNEXT"),  ("5077.KL","JETSON"),    ("3441.KL","JHM"),
    ("5047.KL","JTIASA"),   ("0082.KL","KANGER"),    ("3522.KL","KAB"),
    ("0151.KL","KAWAN"),    ("5235.KL","KEINHIN"),   ("3476.KL","KERJAYA"),
    ("4375.KL","KFC"),      ("7153.KL","KIANJOO"),   ("5027.KL","KKB"),
    ("3492.KL","KLCCP"),    ("9261.KL","KLUANG"),    ("5027.KL","KLBK"),
    ("2445.KL","KLK"),      ("2445.KL","KLKK"),      ("5053.KL","KMAS"),
    ("0048.KL","KNUSFORD"), ("5198.KL","KOBAY2"),    ("5024.KL","KRAMAT"),
    ("2445.KL","KULIM"),    ("3492.KL","KUMPULAN"),  ("5007.KL","LBICAP"),
    ("5136.KL","LAYHONG"),  ("5131.KL","LBCAP"),     ("4006.KL","LBS"),
    ("5029.KL","LCTITAN"),  ("3581.KL","LFECORP"),   ("9288.KL","LHIND"),
    ("4235.KL","LIONDIV"),  ("4359.KL","LIONIND"),   ("7243.KL","LPI"),
    ("5086.KL","LUSTER"),   ("5038.KL","LUXCHEM"),   ("5215.KL","MAGNI"),
    ("3867.KL","MALAKOF"),  ("6012.KL","MALTON"),    ("7081.KL","MAMEE"),
    ("5014.KL","MBF"),      ("5026.KL","MBMR"),      ("5237.KL","MCE"),
    ("3867.KL","MHC"),      ("7229.KL","MIKROMB"),   ("5287.KL","MITRA"),
    ("6012.KL","MKLAND"),   ("7234.KL","MNRB"),      ("5289.KL","MODINS"),
    ("8621.KL","MUHIBAH"),  ("5085.KL","MUDAJAYA"),  ("4677.KL","MUIIND"),
    ("7021.KL","MULPHA"),   ("5079.KL","MY EG"),     ("2054.KL","MYEG"),
    ("5051.KL","MRCB"),     ("6012.KL","MKH"),       ("5065.KL","NETX"),
    ("4715.KL","NAZA"),     ("4383.KL","NESTLE"),    ("5052.KL","NEXNINE"),
    ("5228.KL","NICORP"),   ("3204.KL","NOLBAKI"),   ("5180.KL","NOTION"),
    ("3050.KL","NTPM"),     ("9792.KL","OCK"),       ("7164.KL","OLDTOWN"),
    ("3867.KL","ORION"),    ("5053.KL","ORIENT"),    ("2771.KL","OIB"),
    ("5053.KL","ORIMAS"),   ("5065.KL","OSK"),       ("1066.KL","OSKVI"),
    ("5053.KL","PADINI"),   ("5065.KL","PARKSON"),   ("1724.KL","PARAMON"),
    ("5183.KL","PCGB"),     ("5052.KL","PEB"),       ("4502.KL","PELIKAN"),
    ("9695.KL","PERMAJU"),  ("5053.KL","PETRA"),     ("5015.KL","PINEAPP"),
    ("1724.KL","PJD"),      ("0007.KL","PLACERA"),   ("3689.KL","PLNHLDG"),
    ("5052.KL","POHKONG"),  ("8419.KL","POLY"),      ("5046.KL","POSH"),
    ("4197.KL","PPBG"),     ("5065.KL","PPC"),       ("4065.KL","PPG"),
    ("5052.KL","PRESTAR"),  ("2984.KL","PRICEWORTH"),("5247.KL","PRIVASIA"),
    ("5162.KL","PROTON"),   ("3735.KL","PRTASCO"),   ("4030.KL","PUNCAK"),
    ("7084.KL","QLG"),      ("5016.KL","RCECAP"),    ("5267.KL","REDtone"),
    ("4197.KL","RGTBHD"),   ("7113.KL","RIMBUNAN"),  ("0157.KL","SAPNRG"),
    ("4294.KL","SASBADI"),  ("4294.KL","SBCCORP"),   ("5218.KL","SEACERA"),
    ("9105.KL","SEALINK"),  ("2356.KL","SELANGOR"),  ("4197.KL","SENTRAL"),
    ("5285.KL","SERBA"),    ("4197.KL","SILKHLD"),   ("1961.KL","SIMEPROP"),
    ("4162.KL","SKP"),      ("4162.KL","SKPETRO"),   ("3476.KL","SKYB"),
    ("1589.KL","SLP"),      ("4375.KL","SPB"),       ("5226.KL","SPSETIA"),
    ("1597.KL","SPRITZER"),  ("4405.KL","SREIT"),    ("2836.KL","SUBUR"),
    ("5269.KL","SUNCON"),   ("6521.KL","SUNREIT"),   ("6284.KL","SUNSURIA"),
    ("5196.KL","SUPERMX"),  ("5075.KL","TAMBUN"),    ("8524.KL","TASEK"),
    ("4456.KL","TASCO"),    ("5148.KL","TECGUAN"),   ("3689.KL","TEXCHEM"),
    ("4235.KL","TIMEDOTCOM"),("5178.KL","TOMEI"),    ("5109.KL","TROPICANA"),
    ("2054.KL","TUNEPRO"),  ("5211.KL","UEMS"),      ("2593.KL","UMW"),
    ("7250.KL","UNIMECH"),  ("5005.KL","UOADEV"),    ("5110.KL","VELESTO"),
    ("1171.KL","VITROX"),   ("7216.KL","VSI"),       ("9695.KL","WASEONG"),
    ("3816.KL","WCE"),      ("4677.KL","WCEHB"),     ("5246.KL","WELLCAL"),
    ("3565.KL","WINTONI"),  ("9102.KL","WOODLAND"),  ("4197.KL","XIDELANG"),
    ("5079.KL","XINHWA"),   ("5228.KL","YEELEE"),    ("3948.KL","YILAI"),
    ("1619.KL","ZELAN"),    ("0163.KL","ZHULIAN"),
    # ACE Market
    ("0082.KL","ACE"),      ("0143.KL","AEMULUS"),   ("0152.KL","AGESON"),
    ("0038.KL","AIMFLEX"),  ("0227.KL","AIRPAK"),    ("0072.KL","ALCOM"),
    ("0007.KL","ALEXIS"),   ("0053.KL","ALLIANCEF"),  ("0020.KL","AMCORP"),
    ("0040.KL","AMDB"),     ("0052.KL","AMFIRST"),   ("0024.KL","AMITRADE"),
    ("0003.KL","AMPROP"),   ("0104.KL","ANCOM"),     ("0041.KL","APEX"),
    ("0143.KL","APFT"),     ("0135.KL","APOLLO"),    ("0065.KL","AQRS"),
    ("0013.KL","ARB"),      ("0007.KL","ARCVIEW"),   ("0027.KL","ASIAFILE"),
    ("0227.KL","ASDION"),   ("0163.KL","ATLAN"),     ("0196.KL","ATTA"),
    ("0188.KL","AUTOCAL"),  ("0109.KL","AUXILIO"),   ("0122.KL","AWANTEC"),
    ("0082.KL","BCB"),      ("0010.KL","BCLIND"),    ("0048.KL","BORNOIL"),
    ("0075.KL","BPD"),      ("0099.KL","CEKA"),      ("0143.KL","CENSOF"),
    ("0151.KL","CEPAT"),    ("0007.KL","CLIQ"),      ("0023.KL","CME"),
    ("0082.KL","CNI"),      ("0041.KL","COGENT"),    ("0007.KL","CONNECT"),
    ("0015.KL","CUSCAPI"),  ("0005.KL","CYCLECAR"),  ("0007.KL","DAGANG"),
    ("0143.KL","DATAPRP"),  ("0023.KL","DEGEM"),     ("0029.KL","DESTINI"),
    ("0082.KL","DGSB"),     ("0048.KL","DIALOGSYS"), ("0023.KL","DIGISTAR"),
    ("0082.KL","DKLS"),     ("0076.KL","DPS"),       ("0143.KL","DUOPHARMA"),
    ("0023.KL","E&O"),      ("0041.KL","EFORCE"),    ("0023.KL","EKOVEST2"),
    ("0082.KL","EMCO"),     ("0053.KL","ENGTEX2"),   ("0007.KL","ESCERAM"),
    ("0023.KL","EUROSPAN"), ("0053.KL","EUPE"),       ("0023.KL","EWINT"),
    ("0082.KL","FAJAR"),    ("0007.KL","FELCRA"),    ("0082.KL","FIAMMA2"),
    ("0143.KL","FORMIS"),   ("0023.KL","G3"),         ("0082.KL","GCB2"),
    ("0053.KL","GDEX2"),    ("0143.KL","GESHEN"),    ("0023.KL","GETS"),
    ("0082.KL","GHB"),      ("0023.KL","GLBHD"),     ("0143.KL","GLENEALY"),
    ("0023.KL","GLO"),      ("0082.KL","GOLDTEND"),  ("0082.KL","GPRO"),
    ("0023.KL","GRG"),      ("0143.KL","GUNUNG"),    ("0023.KL","HARVEST"),
    ("0082.KL","HB"),       ("0023.KL","HCK"),       ("0053.KL","HENGLYG"),
    ("0082.KL","HEXATAB"),  ("0023.KL","HIAPTEK"),   ("0053.KL","HIAP"),
    ("0143.KL","HI-TECH"),  ("0082.KL","HIBISCS"),   ("0023.KL","HIGHWAY"),
]


def get_bursa_tickers() -> list[tuple[str, str]]:
    """
    3-layer stock list fetcher:
    1. KLSEScreener API   — live full market list (~1000 stocks)
    2. stocks.json        — bundled verified list (200+ stocks)
    3. BURSA_HARDCODED    — minimal fallback (last resort)
    """
    # Layer 1: KLSEScreener — only accept if full market list returned
    try:
        stocks = _fetch_klsescreener()
        if len(stocks) >= 500:
            log.info(f"✅ Layer 1 (KLSEScreener): {len(stocks)} stocks")
            return stocks
        log.warning(f"Layer 1 only returned {len(stocks)} stocks — too few, skipping to Layer 2")
    except Exception as e:
        log.warning(f"Layer 1 failed: {e}")

    # Layer 2: stocks.json bundled in repo
    try:
        import json, pathlib
        json_path = pathlib.Path(__file__).parent / "stocks.json"
        data   = json.loads(json_path.read_text())
        stocks = [(item["code"], item["name"].replace(".KL","")) for item in data if item.get("code") and item.get("name")]
        if len(stocks) > 500:
            log.info(f"✅ Layer 2 (stocks.json): {len(stocks)} stocks")
            return stocks
    except Exception as e:
        log.warning(f"Layer 2 failed: {e}")

    # Layer 3: CSV file (Bursa_Malaysia.csv)
    try:
        import csv, pathlib
        csv_path = pathlib.Path(__file__).parent / "Bursa_Malaysia.csv"
        stocks = []
        with open(csv_path, 'r') as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    code   = row[0].strip()
                    symbol = row[1].strip().replace('.KL', '')
                    if code and symbol:
                        stocks.append((code, symbol))
        if len(stocks) > 500:
            log.info(f"✅ Layer 3 (CSV): {len(stocks)} stocks")
            return stocks
    except Exception as e:
        log.warning(f"Layer 3 CSV failed: {e}")

    # Layer 4: Hardcoded fallback
    log.info(f"✅ Layer 4 (hardcoded): {len(BURSA_HARDCODED)} stocks")
    return BURSA_HARDCODED


def _fetch_klsescreener() -> list[tuple[str, str]]:
    url     = "https://www.klsescreener.com/v2/screener/quote_results"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.klsescreener.com/v2/screener",
        "Origin":          "https://www.klsescreener.com",
    }
    params = {"board": "", "sector": "", "sortby": "code",
              "sortorder": "asc", "page": 1, "per_page": 9999}
    resp   = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data   = resp.json()
    stocks = [
        (
            f"{item['code']}.KL",
            (item.get("stock_name") or item.get("symbol") or
             item.get("name") or "").strip().split()[0]
        )
        for item in data.get("data", []) if item.get("code")
    ]
    if not stocks:
        raise ValueError("Empty list")
    return stocks


def _generate_bursa_codes() -> list[tuple[str, str]]:
    """Generate all possible 4-digit Bursa codes."""
    ranges = [
        (1, 999), (1000, 1999), (2000, 2999), (3000, 3999),
        (4000, 4999), (5000, 5999), (6000, 6999),
        (7000, 7999), (8000, 8999), (9000, 9999),
    ]
    codes = []
    for start, end in ranges:
        for i in range(start, end + 1):
            codes.append((f"{i:04d}.KL", ""))
    return codes


# ── Signal detection ───────────────────────────────────────────────────────────
def analyze(ticker: str, name: str = "") -> Optional[dict]:
    try:
        # Resolve name from yfinance if missing
        if not name:
            try:
                info = yf.Ticker(ticker).info
                raw  = info.get("shortName", "") or info.get("symbol", "")
                name = raw.split()[0] if raw else ticker.replace(".KL", "")
            except Exception:
                name = ticker.replace(".KL", "")

        # ── Step 1: get real current price via fast_info ─────────────────────
        # fast_info.last_price is the most reliable current price from yfinance
        tk = yf.Ticker(ticker)
        try:
            fi = tk.fast_info
            current_price = float(fi.last_price)
        except Exception:
            return None

        if not current_price or current_price <= 0:
            return None

        # Price range filter: RM0.205 – RM6.90
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            return None

        # ── Step 2: download history for open/close/MA/52WH calculations ─────
        df = tk.history(period="2y", interval="1d", auto_adjust=True)

        if df is None or df.empty or len(df) < 210:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        open_  = df["Open"].dropna()
        close  = df["Close"].dropna()
        high   = df["High"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < 60 or len(open_) < 3:
            return None

        current_volume = float(volume.iloc[-1])

        # Liquidity filter
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        if avg_vol < MIN_VOLUME:
            return None

        # ── Gate: current price must be HIGHER than yesterday's OPEN ──────────
        yesterday_open = float(open_.iloc[-2])
        if yesterday_open <= 0 or current_price <= yesterday_open:
            return None

        # ── Price Up signal: current price ≥7% above close from 2 days ago ───
        close_2d_ago = float(close.iloc[-3])
        pct_vs_2d    = (current_price - close_2d_ago) / close_2d_ago * 100 if close_2d_ago > 0 else 0

        adj_close = close

        # 52WH: rolling 252 days on High prices
        high_252    = float(high.rolling(252).max().iloc[-1])
        pct_to_52wh = (high_252 - current_price) / current_price * 100

        # ATH from full history highs
        ath = float(high.max())

        # ── Golden Cross: MA5 crosses above MA20 ──────────────────────────────
        ma5   = close.rolling(5).mean()
        ma20  = close.rolling(20).mean()
        ma5_now   = float(ma5.iloc[-1])
        ma5_prev  = float(ma5.iloc[-2])
        ma20_now  = float(ma20.iloc[-1])
        ma20_prev = float(ma20.iloc[-2])

        # ── Bullish Zone: price above MA200 ────────────────────────────────────
        ma200_now = float(close.rolling(200).mean().iloc[-1])

        # Build signals
        signals = []

        if pct_vs_2d >= 7.0:
            signals.append(f"📈 Price Up (+{pct_vs_2d:.2f}% vs 2-day-ago close)")
        if ma5_now > ma20_now and ma5_prev <= ma20_prev:
            signals.append("📗 GC Alert")
        if current_price > ma200_now:
            signals.append("📗 Bullish Zone Alert")
        if current_price >= ath * (1 - ATH_TOLERANCE / 100):
            signals.append("📗 ATH Alert")
        if current_price >= high_252 * (1 - WH52_TOLERANCE / 100):
            signals.append("📗 52WH Alert")
        elif 0 < pct_to_52wh <= PENDING_BREAKOUT_PCT:
            signals.append(f"🔥 Pending Breakout ({pct_to_52wh:.1f}% to 52WH)")
        if avg_vol > 0 and current_volume >= avg_vol * VOLUME_SURGE_MULT:
            signals.append("📈 Volume Surge")

        if not signals:
            return None

        return {
            "ticker":  ticker,
            "name":    name,
            "price":   current_price,
            "signals": signals,
        }

    except Exception as e:
        log.debug(f"{ticker}: {e}")
        return None


# ── Main scan ──────────────────────────────────────────────────────────────────
# ── Daily dedup tracking ─────────────────────────────────────────────────────
ALERTED_FILE = "alerted_today.json"


def load_alerted_today() -> set:
    """
    Load the set of tickers already alerted today.
    Resets automatically if the stored date is not today (MYT).
    """
    import json
    import pathlib

    myt   = timezone(timedelta(hours=8))
    today = datetime.now(myt).date().isoformat()

    path = pathlib.Path(__file__).parent / ALERTED_FILE
    if not path.exists():
        return set()

    try:
        data = json.loads(path.read_text())
        if data.get("date") != today:
            # New day — reset
            return set()
        return set(data.get("tickers", []))
    except Exception:
        return set()


def save_alerted_today(alerted: set) -> None:
    """Persist the set of tickers alerted today, tagged with today's date."""
    import json
    import pathlib

    myt   = timezone(timedelta(hours=8))
    today = datetime.now(myt).date().isoformat()

    path = pathlib.Path(__file__).parent / ALERTED_FILE
    path.write_text(json.dumps({
        "date":    today,
        "tickers": sorted(alerted),
    }, indent=2))


def run_scan():
    start = datetime.now()
    log.info("=" * 60)
    log.info(f"Saham Alert starting at {start.strftime('%Y-%m-%d %H:%M')} MYT")
    log.info("=" * 60)

    if not is_market_open():
        return

    stocks = get_bursa_tickers()
    log.info(f"Scanning {len(stocks)} stocks | "
             f"Price RM{MIN_PRICE}–RM{MAX_PRICE} | "
             f"Breakout within {PENDING_BREAKOUT_PCT}% of 52WH")

    results = []
    done    = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze, t, n): (t, n) for t, n in stocks}
        for future in as_completed(futures):
            done += 1
            if done % 100 == 0:
                log.info(f"  Progress: {done}/{len(stocks)}")
            result = future.result()
            if result:
                results.append(result)

    elapsed = (datetime.now() - start).seconds
    log.info(f"Scan complete in {elapsed}s — {len(results)} signals found")

    if not results:
        log.info("No signals this run.")
        return

    # ── Dedup: skip stocks already alerted today ───────────────────────────
    alerted_today = load_alerted_today()
    new_results   = [r for r in results if r["ticker"] not in alerted_today]
    skipped       = len(results) - len(new_results)

    if skipped:
        log.info(f"Skipped {skipped} already-alerted stock(s) today")

    if not new_results:
        log.info("All signals already alerted today — nothing new to send.")
        return

    myt     = timezone(timedelta(hours=8))
    now_myt = datetime.now(myt)
    summary = (
        f"<b>🔍 Saham Alert — Market Scan</b>\n"
        f"{now_myt.strftime('%d %b %Y  %H:%M MYT')}\n"
        f"Scanned: {len(stocks)} stocks\n"
        f"New signals: {len(new_results)}"
        + (f"  (skipped {skipped} repeat)" if skipped else "")
        + f"\n{'─' * 36}"
    )
    send_telegram(summary)
    time.sleep(DELAY_BETWEEN_MSGS)

    for r in new_results:
        msg = format_alert(r["ticker"], r["price"], r["signals"], r.get("name", ""))
        log.info(f"  → {r['ticker']} ({r.get('name','')}) {r['signals']}")
        send_telegram(msg)
        time.sleep(DELAY_BETWEEN_MSGS)
        alerted_today.add(r["ticker"])

    save_alerted_today(alerted_today)
    log.info(f"All alerts sent. {len(alerted_today)} unique stocks alerted today.")


if __name__ == "__main__":
    run_scan()
