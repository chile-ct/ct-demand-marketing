"""
Chotot Marketplace Dashboard — Auto Update Script
Queries BigQuery chotot_mtm + Google Sheets cost data.
No Claude/Anthropic API. $0 token cost.
Run: python3 scripts/update_marketplace.py
"""
import re, os, datetime, subprocess, calendar, urllib.request, json
from google.cloud import bigquery

PROJECT   = "chotot-dwh"
SRC_HTML  = os.path.join(os.path.dirname(__file__), '..', 'src', 'index.html')
DIST_HTML = os.path.join(os.path.dirname(__file__), '..', 'dist', 'index.html')
ROOT_HTML = os.path.join(os.path.dirname(__file__), '..', 'index.html')
SHEETS_ID = "1D-2eQcfDMzy42wHUF4bpwCY4cWtrJNvp-kdv9R_iFUI"
SHEET_TAB = "FC & Actual cost "

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

# ── 3. Google Sheets cost data ──────────────────────────────────────────────
def fetch_growth_cost():
    try:
        gcloud = os.path.expanduser("~/google-cloud-sdk/bin/gcloud")
        token = subprocess.run([gcloud,"auth","print-access-token","--account=chile@chotot.vn"],
                               capture_output=True,text=True).stdout.strip()
        if not token: return None
        enc = SHEET_TAB.replace(' ','%20').replace('&','%26')
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEETS_ID}/values/'{enc}'!A1:R500"
        req = urllib.request.Request(url, headers={"Authorization":f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        rows = data.get('values',[])
        if not rows: return None
        hdr0,hdr1 = rows[0],rows[1]
        mm={'Jan':'2026-01','Feb':'2026-02','Mar':'2026-03','Apr':'2026-04',
            'May':'2026-05','June':'2026-06','Jun':'2026-06','Jul':'2026-07',
            'Aug':'2026-08','Sep':'2026-09','Oct':'2026-10','Nov':'2026-11','Dec':'2026-12'}
        col_info={i:(mm[h],'actual' in str(hdr0[i] if i<len(hdr0) else '').lower())
                  for i,h in enumerate(hdr1) if h in mm}
        def pn(s):
            try: return int(str(s).replace(',','').replace(' ','').replace('\xa0',''))
            except: return 0
        result={}
        for r in rows[2:]:
            row=r+['']*(max(col_info,default=0)+1-len(r))
            v=str(row[0]).strip().upper()
            if v not in ['PTY','JOB','VEH','GDS']: continue
            activity=str(row[2]) if len(row)>2 else ''
            if 'DwL' not in activity: continue  # only DwL campaign spend
            for col,(mk,_) in col_info.items():
                if col<len(row):
                    n=pn(row[col])
                    if n:
                        result.setdefault(v,{}).setdefault(mk,{'total':0})
                        result[v][mk]['total']+=n
        print(f"  Sheets OK — {sum(len(d) for d in result.values())} entries")
        return result
    except Exception as e:
        print(f"  Sheets failed: {e}")
        return None

print("Fetching Google Sheets cost data...")
growth_cost = fetch_growth_cost()

# ── 3b. Detail data (mtm_chotot_vertical_channel) ───────────────────────────
print("Querying detail channel data...")
detail_rows = q(f"""
SELECT
  FORMAT_DATE('%Y-%m', date)  AS m,
  vertical,
  CASE channel
    WHEN 'digital'        THEN 'Growth (Paid)'
    WHEN 'growth_outapp'  THEN 'Growth (CRM)'
    WHEN 'growth_inapp'   THEN 'Growth (CRM)'
    WHEN 'seo'            THEN 'Organic Search'
    WHEN 'direct'         THEN 'Direct'
    ELSE '(Other)'
  END AS channel,
  ROUND(AVG(dau), 0)          AS dau,
  ROUND(AVG(dau_w_lead), 0)   AS dwl,
  SUM(lead_count)             AS lead
FROM `chotot-dwh.ct_digital.mtm_chotot_vertical_channel`
WHERE date BETWEEN '{start}' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND vertical IN ('pty','jobs','veh','gds')
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

def js_detail_data(rows):
    import json as _json
    vert_map = {'pty':'PTY','jobs':'JOB','veh':'VEH','gds':'GDS'}
    lines = ['// Detail channel data — auto-updated from daumaulead_mkt_rp',
             'const RAW = [']
    for r in rows:
        v = vert_map.get(str(r.get('vertical','')).lower())
        if not v: continue
        ch = _json.dumps(str(r.get('channel','')))
        dau  = int(r['dau'])  if r['dau']  is not None else 0
        dwl  = int(r['dwl'])  if r['dwl']  is not None else 0
        lead = int(r['lead']) if r['lead'] is not None else 0
        lines.append(f'  {{v:"{v}",ch:{ch},m:"{r["m"]}",dau:{dau},dwl:{dwl},lead:{lead}}},')
    lines.append('];')
    return '\n'.join(lines)

def js_growth_cost(cost):
    lines=['// Cost data — auto-updated from Google Sheets "FC & Actual cost"',
           '// actual = accrued | forecast = planned (VND)','const GROWTH_COST = {']
    for v in ['PTY','JOB','VEH','GDS']:
        lines.append(f'  {v}:{{')
        for mk,d in sorted(cost.get(v,{}).items()):
            lines.append(f'    "{mk}":{d["total"]},')
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
if growth_cost:
    html=re.sub(r'// Cost data[\s\S]*?const GROWTH_COST = \{[\s\S]*?^};',js_growth_cost(growth_cost),html,flags=re.MULTILINE)
    print("  GROWTH_COST updated from Sheets")
_detail_js = js_detail_data(detail_rows)
html=re.sub(r'// Detail channel data[\s\S]*?const RAW = \[[\s\S]*?^];', lambda _: _detail_js, html, flags=re.MULTILINE)
print("  RAW (detail) updated from daumaulead_mkt_rp")
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
