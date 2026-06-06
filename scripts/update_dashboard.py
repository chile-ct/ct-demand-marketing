"""
Chotot Marketplace Dashboard — Auto Update Script
Queries BigQuery chotot_mtm tables for latest data, patches src/index.html, then calls node build.js.
No Claude/Anthropic API. $0 token cost.

Run: python3 scripts/update_dashboard.py
"""
import re, os, datetime, subprocess, calendar, urllib.request, json
from google.cloud import bigquery

PROJECT   = "chotot-dwh"
SRC_HTML  = os.path.join(os.path.dirname(__file__), '..', 'src', 'index.html')
DIST_HTML = os.path.join(os.path.dirname(__file__), '..', 'dist', 'index.html')
ROOT_HTML = os.path.join(os.path.dirname(__file__), '..', 'index.html')

client = bigquery.Client(project=PROJECT)

def q(sql):
    return [dict(r) for r in client.query(sql).result()]

def fmt_m(date_val):
    """date → '2026-05'"""
    if isinstance(date_val, str): return date_val[:7]
    return date_val.strftime('%Y-%m')

def days_in_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return calendar.monthrange(y, m)[1]

# ── 1. Find latest available month ─────────────────────────────────────────────
print("Finding latest month in BQ...")
latest_rows = q("""
    SELECT FORMAT_DATE('%Y-%m', MAX(date)) AS latest
    FROM `chotot-dwh.chotot_mtm.dashboard__dau_vertical_daily`
""")
latest_m = latest_rows[0]['latest']
print(f"  Latest month: {latest_m}")

# Build months T1/2026 → latest
year = int(latest_m[:4])
last_mo = int(latest_m[5:7])
months = [f"{year}-{m:02d}" for m in range(1, last_mo + 1)]
print(f"  Months: {months}")

# ── 2. Query all metrics ────────────────────────────────────────────────────────
m_range_start = f"{months[0]}-01"
m_range_end   = f"{latest_m}-31"

print("Querying DAU by vertical...")
dau_rows = q(f"""
    SELECT FORMAT_DATE('%Y-%m', date) AS month, vertical,
           ROUND(AVG(dau), 0) AS avg_dau
    FROM `chotot-dwh.chotot_mtm.dashboard__dau_vertical_daily`
    WHERE date BETWEEN '{m_range_start}' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    GROUP BY 1, 2 ORDER BY 1, 2
""")

print("Querying MAU by vertical...")
mau_rows = q(f"""
    SELECT FORMAT_DATE('%Y-%m', date) AS month, vertical, dau AS mau
    FROM `chotot-dwh.chotot_mtm.dashboard__dau_vertical_monthly`
    WHERE date BETWEEN '{m_range_start}' AND '{latest_m}-01'
    ORDER BY 1, 2
""")

print("Querying DwL + Lead by vertical...")
dwl_rows = q(f"""
    SELECT FORMAT_DATE('%Y-%m', date) AS month, vertical,
           ROUND(dauwlead, 0) AS dwl, lead, mauwlead
    FROM `chotot-dwh.chotot_mtm.dashboard__dauwlead__vertical_monthly`
    WHERE date BETWEEN '{m_range_start}' AND '{latest_m}-01'
    ORDER BY 1, 2
""")

print("Querying MAU w/Lead...")
maulead_rows = q(f"""
    SELECT FORMAT_DATE('%Y-%m', date) AS month, vertical, mauwlead
    FROM `chotot-dwh.chotot_mtm.dashboard__mauwlead__vertical_monthly`
    WHERE date BETWEEN '{m_range_start}' AND '{latest_m}-01'
    ORDER BY 1, 2
""")

# ── 3. Build lookup dicts ───────────────────────────────────────────────────────
BQ_VERTS = {'pty':'PTY', 'jobs':'JOB', 'veh':'VEH', 'gds':'GDS'}

def build_vert_act():
    data = {v: {} for v in ['PTY','JOB','VEH','GDS']}
    for r in dau_rows:
        v = BQ_VERTS.get(r['vertical'])
        if v: data[v].setdefault(r['month'], {})['dau'] = int(r['avg_dau'])
    for r in mau_rows:
        v = BQ_VERTS.get(r['vertical'])
        if v: data[v].setdefault(r['month'], {})['mau'] = int(r['mau'])
    for r in dwl_rows:
        v = BQ_VERTS.get(r['vertical'])
        if v:
            data[v].setdefault(r['month'], {})['dwl']  = int(r['dwl'])
            data[v].setdefault(r['month'], {})['lead'] = int(r['lead'])
    for r in maulead_rows:
        v = BQ_VERTS.get(r['vertical'])
        if v: data[v].setdefault(r['month'], {})['mauLead'] = int(r['mauwlead'])
    return data

def build_act(vert_act):
    """Platform totals from 'all' vertical rows"""
    act = {}
    dau_all  = {r['month']: int(r['avg_dau']) for r in dau_rows  if r['vertical']=='all'}
    mau_all  = {r['month']: int(r['mau'])     for r in mau_rows  if r['vertical']=='all'}
    dwl_all  = {r['month']: int(r['dwl'])     for r in dwl_rows  if r['vertical']=='all'}
    lead_all = {r['month']: int(r['lead'])    for r in dwl_rows  if r['vertical']=='all'}
    mauL_all = {r['month']: int(r['mauwlead'])for r in maulead_rows if r['vertical']=='all'}
    for m in months:
        dau  = dau_all.get(m, 0)
        mau  = mau_all.get(m, 0)
        dwl  = dwl_all.get(m, 0)
        lead = lead_all.get(m, 0)
        mauL = mauL_all.get(m, 0)
        dauMau = round(dau/mau*100, 1) if mau else 0
        act[m] = dict(dau=dau, dwl=dwl, lead=lead, mau=mau, mauLead=mauL, dauMau=dauMau)
    return act

vert_act = build_vert_act()
act = build_act(vert_act)

# ── 3b. Fetch GROWTH_COST from Google Sheets ───────────────────────────────────
SHEETS_ID = "1D-2eQcfDMzy42wHUF4bpwCY4cWtrJNvp-kdv9R_iFUI"
SHEET_NAME = "FC & Actual cost "

def fetch_growth_cost():
    """Fetch cost data from Google Sheets using ADC token. Returns {vert: {month: cost}}"""
    import subprocess as sp
    try:
        gcloud = os.path.expanduser("~/google-cloud-sdk/bin/gcloud")
        token = sp.run(
            [gcloud, "auth", "print-access-token", "--account=chile@chotot.vn"],
            capture_output=True, text=True
        ).stdout.strip()
        if not token:
            print("  WARNING: No gcloud token — GROWTH_COST not updated")
            return None

        range_enc = SHEET_NAME.replace(' ', '%20').replace('&', '%26')
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEETS_ID}/values/'{range_enc}'!A1:R500"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        rows = data.get('values', [])
        if not rows:
            print("  WARNING: Empty Sheets response")
            return None

        hdr0 = rows[0]   # actual/forecast row
        hdr1 = rows[1]   # month names row
        month_map = {
            'Jan':'2026-01','Feb':'2026-02','Mar':'2026-03','Apr':'2026-04',
            'May':'2026-05','June':'2026-06','Jun':'2026-06','Jul':'2026-07',
            'Aug':'2026-08','Sep':'2026-09','Oct':'2026-10','Nov':'2026-11','Dec':'2026-12'
        }
        col_info = {}
        for i, h in enumerate(hdr1):
            if h in month_map:
                is_actual = i < len(hdr0) and 'actual' in str(hdr0[i]).lower()
                col_info[i] = (month_map[h], is_actual)

        def parse_num(s):
            try: return int(str(s).replace(',', '').replace(' ', '').replace('\xa0', ''))
            except: return 0

        result = {}
        for r in rows[2:]:
            row = r + [''] * (max(col_info.keys(), default=0) + 1 - len(r))
            vert = str(row[0]).strip().upper()
            if vert not in ['PTY','JOB','VEH','GDS']:
                continue
            for col, (mk, is_act) in col_info.items():
                if col < len(row):
                    v = parse_num(row[col])
                    if v:
                        result.setdefault(vert, {}).setdefault(mk, {'total': 0, 'is_actual': is_act})
                        result[vert][mk]['total'] += v

        print(f"  Sheets OK — {sum(len(v) for v in result.values())} month entries across {len(result)} verticals")
        return result

    except Exception as e:
        print(f"  WARNING: Sheets fetch failed — {e}")
        return None

def js_growth_cost(cost_data):
    """Build GROWTH_COST JS constant from Sheets data."""
    lines = [
        '// Cost data — auto-updated from Google Sheets "FC & Actual cost"',
        '// actual = accrued spend | forecast = planned budget (VND)',
        'const GROWTH_COST = {'
    ]
    for vert in ['PTY','JOB','VEH','GDS']:
        lines.append(f'  {vert}:{{')
        for mk, d in sorted(cost_data.get(vert, {}).items()):
            lines.append(f'    "{mk}":{d["total"]},')
        lines.append('  },')
    lines.append('};')
    return '\n'.join(lines)

print("Fetching cost data from Google Sheets...")
growth_cost_data = fetch_growth_cost()

# ── 4. Build JS constants ───────────────────────────────────────────────────────
def js_months():
    arr = ', '.join(f'"{m}"' for m in months)
    ml  = ', '.join(f'"{m}":"T{int(m[5:])}/26{"*" if m==latest_m else ""}"' for m in months)
    return f'const MONTHS   = [{arr}];', f'const ML       = {{{ml}}};'

def js_vert_act():
    lines = ['// Per-vertical actuals — auto-updated by update_dashboard.py',
             'const VERT_ACT = {']
    for vert in ['PTY','JOB','VEH','GDS']:
        lines.append(f'  {vert}:{{')
        for m, d in sorted(vert_act.get(vert,{}).items()):
            dau  = d.get('dau',0)
            dwl  = d.get('dwl',0)
            lead = d.get('lead',0)
            mau  = d.get('mau',0)
            mauL = d.get('mauLead',0)
            lines.append(f'    "{m}":{{dau:{dau},dwl:{dwl},lead:{lead},mau:{mau},mauLead:{mauL}}},')
        lines.append('  },')
    lines.append('};')
    return '\n'.join(lines)

def js_act():
    lines = ['// Total platform actuals — auto-updated by update_dashboard.py',
             'const ACT = {']
    for m in months:
        d = act[m]
        lines.append(f'  "{m}":{{dau:{d["dau"]},dwl:{d["dwl"]},lead:{d["lead"]},'
                     f'mau:{d["mau"]},mauLead:{d["mauLead"]},dauMau:{d["dauMau"]}}},')
    lines.append('};')
    return '\n'.join(lines)

def js_act_mau():
    lines = ['const ACT_MAU = {']
    for m in months:
        mau = act[m]['mau']
        lines.append(f'  "{m}":{{mau:{mau},newMau:0,retMau:0}},')
    lines.append('};')
    return '\n'.join(lines)

# ── 5. Patch src/index.html ─────────────────────────────────────────────────────
print("Patching src/index.html...")
with open(SRC_HTML, 'r') as f:
    html = f.read()

# Replace MONTHS
months_js, ml_js = js_months()
html = re.sub(r'const MONTHS\s*=\s*\[.*?\];', months_js, html)
html = re.sub(r'const ML\s*=\s*\{.*?\};', ml_js, html)

# Replace VERT_ACT block
html = re.sub(
    r'// Per-vertical actuals.*?const VERT_ACT = \{[\s\S]*?^};',
    js_vert_act(),
    html, flags=re.MULTILINE
)

# Replace ACT block
html = re.sub(
    r'// Total platform actuals.*?const ACT = \{[\s\S]*?^};',
    js_act(),
    html, flags=re.MULTILINE
)

# Replace ACT_MAU
html = re.sub(
    r'const ACT_MAU = \{[\s\S]*?^};',
    js_act_mau(),
    html, flags=re.MULTILINE
)

# Replace GROWTH_COST if Sheets data available
if growth_cost_data:
    html = re.sub(
        r'// Cost data.*?const GROWTH_COST = \{[\s\S]*?^};',
        js_growth_cost(growth_cost_data),
        html, flags=re.MULTILINE
    )
    print(f"  GROWTH_COST patched from Sheets")
else:
    print(f"  GROWTH_COST kept as-is (Sheets unavailable)")

with open(SRC_HTML, 'w') as f:
    f.write(html)
print(f"  Patched: {len(months)} months, latest={latest_m}")

# ── 6. Build dist/ ──────────────────────────────────────────────────────────────
print("Building dist/index.html...")
result = subprocess.run(['node', 'build.js'],
                        capture_output=True, text=True,
                        cwd=os.path.dirname(SRC_HTML).replace('/src',''))
if result.returncode != 0:
    print("❌ Build failed:", result.stderr)
    raise SystemExit(1)
print(result.stdout.strip())

# Copy to root for GitHub Pages
import shutil
shutil.copy(DIST_HTML, ROOT_HTML)
print(f"✅ Done — {latest_m} is latest, {len(months)} months total")
