"""
Chotot Marketplace Dashboard — Auto Update Script
Queries BigQuery chotot_mtm + Google Sheets cost data.
No Claude/Anthropic API. $0 token cost.
Run: python3 scripts/update_marketplace.py
"""
import re, os, datetime, subprocess, urllib.request, json
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT   = "chotot-dwh"
SRC_HTML  = os.path.join(os.path.dirname(__file__), '..', 'src', 'index.html')
DIST_HTML = os.path.join(os.path.dirname(__file__), '..', 'dist', 'index.html')
ROOT_HTML = os.path.join(os.path.dirname(__file__), '..', 'index.html')

# BQ client with Drive scope so Sheets-linked tables (kiet_gg_ad_cost) are accessible
_SCOPES = [
    'https://www.googleapis.com/auth/bigquery',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/cloud-platform',
]
_creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
if _creds_path:
    _creds = service_account.Credentials.from_service_account_file(_creds_path, scopes=_SCOPES)
    client = bigquery.Client(project=PROJECT, credentials=_creds)
else:
    client = bigquery.Client(project=PROJECT)

def q(sql): return [dict(r) for r in client.query(sql).result()]
def fmt_m(v): return v.strftime('%Y-%m') if hasattr(v,'strftime') else str(v)[:7]

# ── 1. Latest month ─────────────────────────────────────────────────────────
print("Finding latest month...")
latest_m = q("SELECT FORMAT_DATE('%Y-%m', MAX(date)) AS m FROM `chotot-dwh.chotot_mtm.dashboard__dau_vertical_daily`")[0]['m']
year = int(latest_m[:4]); last_mo = int(latest_m[5:7])
months = [f"{year}-{m:02d}" for m in range(1, last_mo+1)]
print(f"  Latest: {latest_m}, months: {months}")

# ── 2. BQ queries ────────────────────────────────────────────────────────────
start, end = f"{months[0]}-01", f"{latest_m}-31"
print("Querying BQ...")
dau_rows  = q(f"SELECT FORMAT_DATE('%Y-%m',date) m,vertical,ROUND(AVG(dau),0) avg_dau FROM `chotot-dwh.chotot_mtm.dashboard__dau_vertical_daily` WHERE date BETWEEN '{start}' AND DATE_SUB(CURRENT_DATE(),INTERVAL 1 DAY) GROUP BY 1,2 ORDER BY 1,2")
mau_rows  = q(f"SELECT FORMAT_DATE('%Y-%m',date) m,vertical,dau mau FROM `chotot-dwh.chotot_mtm.dashboard__dau_vertical_monthly` WHERE date BETWEEN '{start}' AND '{latest_m}-01' ORDER BY 1,2")
dwl_rows  = q(f"SELECT FORMAT_DATE('%Y-%m',date) m,vertical,ROUND(dauwlead,0) dwl,lead,mauwlead FROM `chotot-dwh.chotot_mtm.dashboard__dauwlead__vertical_monthly` WHERE date BETWEEN '{start}' AND '{latest_m}-01' ORDER BY 1,2")
# Use dauwlead table for mauLead (matches monthly report source; mauwlead table has slight dedup diff)
mauL_rows = [{'m':r['m'],'vertical':r['vertical'],'mauwlead':r['mauwlead']} for r in dwl_rows]

BQ_V = {'pty':'PTY','jobs':'JOB','veh':'VEH','gds':'GDS'}

def build_vert_act():
    d = {v:{} for v in ['PTY','JOB','VEH','GDS']}
    for r in dau_rows:
        v=BQ_V.get(r['vertical'])
        if v: d[v].setdefault(r['m'],{})['dau']=int(r['avg_dau'])
    for r in mau_rows:
        v=BQ_V.get(r['vertical'])
        if v: d[v].setdefault(r['m'],{})['mau']=int(r['mau'])
    for r in dwl_rows:
        v=BQ_V.get(r['vertical'])
        if v:
            d[v].setdefault(r['m'],{})['dwl']=int(r['dwl'])
            d[v].setdefault(r['m'],{})['lead']=int(r['lead'])
    for r in mauL_rows:
        v=BQ_V.get(r['vertical'])
        if v: d[v].setdefault(r['m'],{})['mauLead']=int(r['mauwlead'])
    return d

def build_act(vert_act):
    dau_a ={r['m']:int(r['avg_dau']) for r in dau_rows  if r['vertical']=='all'}
    mau_a ={r['m']:int(r['mau'])     for r in mau_rows  if r['vertical']=='all'}
    dwl_a ={r['m']:int(r['dwl'])     for r in dwl_rows  if r['vertical']=='all'}
    lead_a={r['m']:int(r['lead'])    for r in dwl_rows  if r['vertical']=='all'}
    mauL_a={r['m']:int(r['mauwlead'])for r in mauL_rows if r['vertical']=='all'}
    act={}
    for m in months:
        dau=dau_a.get(m,0); mau=mau_a.get(m,0)
        act[m]=dict(dau=dau,dwl=dwl_a.get(m,0),lead=lead_a.get(m,0),
                    mau=mau,mauLead=mauL_a.get(m,0),dauMau=round(dau/mau*100,1) if mau else 0)
    return act

vert_act = build_vert_act()
act = build_act(vert_act)

# ── 3. Actual Growth Paid cost from BQ (gg + meta, completed months only) ───
def fetch_growth_cost_bq(start_date, end_date):
    """Query actual spend from kiet_gg_ad_cost (GG) + meta_ads_campaign (Meta).
    Returns {vertical: {month: cost_vnd}} for completed months only."""
    try:
        rows = q(f"""
        WITH gg_cost AS (
          SELECT Day AS date,
            SUM(CASE WHEN Account_Name IN ('Chotot_PTY_DAU_New','Chotot_pty_sgd')          THEN Cost ELSE 0 END) AS pty,
            SUM(CASE WHEN Account_Name IN ('Chotot_JOB_VND','Chotot_job_sgd')              THEN Cost ELSE 0 END) AS job,
            SUM(CASE WHEN Account_Name IN ('Chotot_VEH_DAU_New','Chotot_veh_sgd')          THEN Cost ELSE 0 END) AS veh,
            SUM(CASE WHEN Account_Name IN ('Chotot_GDS_ELT_DAU_New','Chotot_gds_elt_sgd')  THEN Cost ELSE 0 END) AS gds
          FROM `chotot-dwh.ct_digital.kiet_gg_ad_cost`
          GROUP BY Day
        ),
        meta_cost AS (
          SELECT date_start AS date,
            SUM(CASE WHEN advertiser_name IN ('Chotot_PTY_DAU_New','Chotot_pty_sgd')         THEN spend*20377 ELSE 0 END) AS pty,
            SUM(CASE WHEN advertiser_name IN ('Chotot_JOB_VND','Chotot_job_sgd')             THEN spend*20377 ELSE 0 END) AS job,
            SUM(CASE WHEN advertiser_name IN ('Chotot_VEH_DAU_New','Chotot_veh_sgd')         THEN spend*20377 ELSE 0 END) AS veh,
            SUM(CASE WHEN advertiser_name IN ('Chotot_GDS_ELT_DAU_New','Chotot_gds_elt_sgd') THEN spend*20377 ELSE 0 END) AS gds
          FROM `chotot-dwh.chotot_marketing.meta_ads_campaign`
          GROUP BY date_start
        )
        SELECT
          FORMAT_DATE('%Y-%m', COALESCE(gg.date, meta.date)) AS month,
          ROUND(SUM(COALESCE(gg.pty,0) + COALESCE(meta.pty,0)), 0) AS pty,
          ROUND(SUM(COALESCE(gg.job,0) + COALESCE(meta.job,0)), 0) AS job,
          ROUND(SUM(COALESCE(gg.veh,0) + COALESCE(meta.veh,0)), 0) AS veh,
          ROUND(SUM(COALESCE(gg.gds,0) + COALESCE(meta.gds,0)), 0) AS gds
        FROM gg_cost gg
        FULL OUTER JOIN meta_cost meta ON gg.date = meta.date
        WHERE COALESCE(gg.date, meta.date) BETWEEN '{start_date}' AND '{end_date}'
        GROUP BY month
        ORDER BY month
        """)
        result = {}
        for r in rows:
            m = r['month']
            for v, col in [('PTY','pty'),('JOB','job'),('VEH','veh'),('GDS','gds')]:
                val = int(r[col] or 0)
                if val > 0:
                    result.setdefault(v, {})[m] = val
        print(f"  BQ cost OK — {sum(len(d) for d in result.values())} month-vertical entries")
        return result
    except Exception as e:
        print(f"  BQ cost failed: {e}")
        return {}

print("Querying actual Growth Paid cost from BQ...")
# Only fetch completed months (exclude current partial month)
today = datetime.date.today()
last_complete = (today.replace(day=1) - datetime.timedelta(days=1))  # last day of prev month
cost_actual = fetch_growth_cost_bq(f"{months[0]}-01", last_complete.strftime('%Y-%m-%d'))
print(f"  Actual cost loaded for months up to {last_complete.strftime('%Y-%m')}")

# ── 3b. Detail data (daumaulead_mkt_rp) ──────────────────────────────────────
print("Querying detail channel data...")
detail_rows = q(f"""
SELECT
  FORMAT_DATE('%Y-%m', date) AS m,
  vertical,
  channel,
  ROUND(AVG(daily_dau), 0)          AS dau,
  ROUND(AVG(daily_new_dau), 0)      AS new_dau,
  ROUND(AVG(daily_ret_dau), 0)      AS ret_dau,
  ROUND(AVG(daily_dwl), 0)          AS dwl,
  SUM(daily_lead)                   AS lead,
  ROUND(AVG(daily_new_mau_lead), 0) AS new_mau_lead,
  ROUND(AVG(daily_ret_mau_lead), 0) AS ret_mau_lead,
  ROUND(AVG(daily_mau), 0)          AS mau_ch,
  ROUND(AVG(daily_mau_lead), 0)     AS mau_lead_ch
FROM (
  SELECT
    date, vertical,
    CASE channel
      WHEN 'Referral' THEN '(Other)'
      WHEN 'Social'   THEN '(Other)'
      WHEN 'Others'   THEN '(Other)'
      ELSE channel
    END                      AS channel,
    SUM(dau)                 AS daily_dau,
    SUM(new_dau)             AS daily_new_dau,
    SUM(return_dau)          AS daily_ret_dau,
    SUM(dau_w_lead)          AS daily_dwl,
    SUM(lead_daily)          AS daily_lead,
    SUM(new_mau_w_lead)      AS daily_new_mau_lead,
    SUM(return_mau_w_lead)   AS daily_ret_mau_lead,
    SUM(mau)                 AS daily_mau,
    SUM(mau_w_lead)          AS daily_mau_lead
  FROM `chotot-dwh.ct_product_analytics.daumaulead_mkt_rp`
  WHERE date BETWEEN '{start}' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    AND vertical IN ('pty','jobs','veh','gds')
    AND platform IS NOT NULL
  GROUP BY 1, 2, 3
)
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
""")
print(f"  Detail rows: {len(detail_rows)}")

# ── 3c. Campaign data (BQ) ───────────────────────────────────────────────────
print("Querying campaign data...")
camp_rows = q(f"""
SELECT
  FORMAT_DATE('%Y-%m', date) AS m,
  vertical,
  channel,
  campaign,
  ROUND(AVG(dau), 0)        AS dau,
  ROUND(AVG(dau_w_lead), 0) AS dwl,
  MAX(lead_mth)              AS lead
FROM `chotot-dwh.ct_digital.mtm_chotot_vertical_channel_campaign_dau_mau`
WHERE date BETWEEN '{start}' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND channel IN ('digital', 'growth_outapp')
  AND campaign NOT IN ('(none)','(not set)','(organic)','(referral)')
  AND vertical IN ('pty - Total','jobs - Total','veh - Total','gds - Total')
GROUP BY 1, 2, 3, 4
ORDER BY 1, vertical, channel, dwl DESC
""")
print(f"  Campaign rows: {len(camp_rows)}")

# ── 4. JS generators ─────────────────────────────────────────────────────────
def js_months():
    arr=', '.join(f'"{m}"' for m in months)
    ml=', '.join(f'"{m}":"T{int(m[5:])}/26{"*" if m==latest_m else ""}"' for m in months)
    return f'const MONTHS   = [{arr}];', f'const ML       = {{{ml}}};'

def js_vert_act():
    lines=['// Per-vertical actuals — auto-updated by update_marketplace.py','const VERT_ACT = {']
    for v in ['PTY','JOB','VEH','GDS']:
        lines.append(f'  {v}:{{')
        for m,d in sorted(vert_act.get(v,{}).items()):
            lines.append(f'    "{m}":{{dau:{d.get("dau",0)},dwl:{d.get("dwl",0)},lead:{d.get("lead",0)},mau:{d.get("mau",0)},mauLead:{d.get("mauLead",0)}}},')
        lines.append('  },')
    lines.append('};')
    return '\n'.join(lines)

def js_act():
    lines=['// Total platform actuals — auto-updated by update_marketplace.py','const ACT = {']
    for m in months:
        d=act[m]
        lines.append(f'  "{m}":{{dau:{d["dau"]},dwl:{d["dwl"]},lead:{d["lead"]},mau:{d["mau"]},mauLead:{d["mauLead"]},dauMau:{d["dauMau"]}}},')
    lines.append('};')
    return '\n'.join(lines)

def js_act_mau():
    lines=['const ACT_MAU = {']
    for m in months:
        lines.append(f'  "{m}":{{mau:{act[m]["mau"]},newMau:0,retMau:0}},')
    lines.append('};')
    return '\n'.join(lines)

def map_vertical(raw):
    """Map BQ vertical string (e.g. 'pty - Total', '2020 - Total') to PTY/JOB/VEH/GDS."""
    if not raw: return 'GDS'
    key = str(raw).split(' - ')[0].strip().lower()
    # Text-based verticals
    text_map = {'pty':'PTY','jobs':'JOB','veh':'VEH','gds':'GDS','c2c':'GDS','elt':'GDS'}
    if key in text_map: return text_map[key]
    # Numeric category codes
    try:
        code = int(key)
        if 1000 <= code <= 1050: return 'PTY'
        if 2000 <= code <= 2080: return 'VEH'
        if code in (13000, 13010): return 'JOB'
        return 'GDS'
    except ValueError:
        return 'GDS'

def js_campaign_data(rows):
    import json as _json
    ch_map = {'digital':'Paid','growth_outapp':'CRM'}
    lines = ['// Campaign data — auto-updated by update_marketplace.py',
             'const CAMPAIGN_DATA = [']
    for r in rows:
        ch = ch_map.get(r['channel'],'')
        if not ch: continue
        v    = map_vertical(r.get('vertical',''))
        camp = _json.dumps(str(r['campaign']))
        dau  = int(r['dau'])  if r['dau']  is not None else 0
        dwl  = int(r['dwl'])  if r['dwl']  is not None else 0
        lead = int(r['lead']) if r['lead'] is not None else 0
        lines.append(f'  {{m:"{r["m"]}",v:"{v}",ch:"{ch}",campaign:{camp},dau:{dau},dwl:{dwl},lead:{lead}}},')
    lines.append('];')
    return '\n'.join(lines)

def js_detail_data(detail_rows, camp_rows):
    import json as _json
    vert_map = {'pty':'PTY','jobs':'JOB','veh':'VEH','gds':'GDS'}

    lines = ['// Detail channel data — auto-updated from daumaulead_mkt_rp (SUM platforms/day → AVG/month)',
             'const RAW = [']
    for r in detail_rows:
        v = vert_map.get(str(r.get('vertical','')).lower())
        if not v: continue
        ch = str(r.get('channel',''))
        dau  = int(r['dau'])          if r['dau']          is not None else 0
        nd   = int(r['new_dau'])      if r['new_dau']      is not None else 0
        rd   = int(r['ret_dau'])      if r['ret_dau']      is not None else 0
        dwl  = int(r['dwl'])          if r['dwl']          is not None else 0
        lead = int(r['lead'])         if r['lead']         is not None else 0
        nm   = int(r['new_mau_lead']) if r['new_mau_lead'] is not None else 0
        rm   = int(r['ret_mau_lead']) if r['ret_mau_lead'] is not None else 0
        mc   = int(r['mau_ch'])       if r['mau_ch']       is not None else 0
        ml   = int(r['mau_lead_ch'])  if r['mau_lead_ch']  is not None else 0
        lines.append(f'  {{v:"{v}",ch:{_json.dumps(ch)},m:"{r["m"]}",dau:{dau},nd:{nd},rd:{rd},dwl:{dwl},lead:{lead},nm:{nm},rm:{rm},mc:{mc},ml:{ml}}},')
    lines.append('];')
    return '\n'.join(lines)

def js_growth_cost(actual, existing_html):
    """Merge actual BQ cost into existing GROWTH_COST, keeping budget plan for future months."""
    # Parse existing GROWTH_COST from HTML to preserve budget values
    existing = {}
    m = re.search(r'const GROWTH_COST = \{([\s\S]*?)^};', existing_html, re.MULTILINE)
    if m:
        for vert_m in re.finditer(r'(\w+):\{([\s\S]*?)\}', m.group(1)):
            v = vert_m.group(1)
            for entry in re.finditer(r'"([\d-]+)"\s*:\s*(\d+)', vert_m.group(2)):
                existing.setdefault(v, {})[entry.group(1)] = int(entry.group(2))
    # Overlay actual on top of existing
    merged = {}
    for v in ['PTY','JOB','VEH','GDS']:
        merged[v] = dict(existing.get(v, {}))
        for mk, val in actual.get(v, {}).items():
            merged[v][mk] = val
    lines = ['// Cost data — actual from BQ (gg+meta) for completed months; budget plan for future',
             '// auto-updated by update_marketplace.py', 'const GROWTH_COST = {']
    for v in ['PTY','JOB','VEH','GDS']:
        lines.append(f'  {v}:{{')
        for mk in sorted(merged.get(v, {})):
            lines.append(f'    "{mk}":{merged[v][mk]},')
        lines.append('  },')
    lines.append('};')
    return '\n'.join(lines)

# ── 5. Patch src/index.html ──────────────────────────────────────────────────
print("Patching src/index.html...")
with open(SRC_HTML) as f: html=f.read()

mjs,mljs=js_months()
html=re.sub(r'const MONTHS\s*=\s*\[.*?\];',mjs,html)
html=re.sub(r'const ML\s*=\s*\{.*?\};',mljs,html)
html=re.sub(r'// Per-vertical actuals[\s\S]*?const VERT_ACT = \{[\s\S]*?^};',js_vert_act(),html,flags=re.MULTILINE)
html=re.sub(r'// Total platform actuals[\s\S]*?const ACT = \{[\s\S]*?^};',js_act(),html,flags=re.MULTILINE)
html=re.sub(r'const ACT_MAU = \{[\s\S]*?^};',js_act_mau(),html,flags=re.MULTILINE)
# Remove old Object.assign overrides
html=re.sub(r'\n?//\s*T\d+ partial[^\n]*\n(Object\.assign\(VERT_ACT\.[A-Z]+[^\n]*\n)*','\n',html)
html=re.sub(r'Object\.assign\(VERT_ACT\.[A-Z]+[^\n]*\n','',html)
if cost_actual:
    html=re.sub(r'// Cost data[\s\S]*?const GROWTH_COST = \{[\s\S]*?^};',js_growth_cost(cost_actual,html),html,flags=re.MULTILINE)
    print(f"  GROWTH_COST updated — actual for completed months, budget kept for future")
_detail_js = js_detail_data(detail_rows, camp_rows)
html=re.sub(r'// Detail channel data[\s\S]*?const RAW = \[[\s\S]*?^];', lambda _: _detail_js, html, flags=re.MULTILINE)
print("  RAW (detail) updated from daumaulead_mkt_rp (all channels, SUM platforms/day)")
_camp_js = js_campaign_data(camp_rows)
html=re.sub(r'// Campaign data[\s\S]*?const CAMPAIGN_DATA = \[[\s\S]*?^];', lambda _: _camp_js, html, flags=re.MULTILINE)
print("  CAMPAIGN_DATA updated")

# Patch DATA_AS_OF
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
html=re.sub(r'const DATA_AS_OF = "[^"]*"',f'const DATA_AS_OF = "{yesterday}"',html)

with open(SRC_HTML,'w') as f: f.write(html)
print(f"  Patched: {len(months)} months, latest={latest_m}")

# ── 6. Build ─────────────────────────────────────────────────────────────────
print("Building...")
res=subprocess.run(['node','build.js'],capture_output=True,text=True,
                   cwd=os.path.dirname(SRC_HTML).replace('/src',''))
if res.returncode!=0: print("❌ Build failed:",res.stderr); raise SystemExit(1)
print(res.stdout.strip())
import shutil; shutil.copy(DIST_HTML,ROOT_HTML)
print(f"✅ Done — latest={latest_m}, {len(months)} months")
