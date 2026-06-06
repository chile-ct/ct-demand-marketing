"""
App Growth Dashboard — Auto Update Script
Queries BigQuery directly. No Claude/Anthropic API. $0 token cost.
"""
import json, os, datetime
from google.cloud import bigquery

PROJECT = "chotot-dwh"
DATA_JSON = os.path.join(os.path.dirname(__file__), '..', 'data.json')

client = bigquery.Client(project=PROJECT)

def run(sql):
    return [dict(r) for r in client.query(sql).result()]

def to_date(val):
    if isinstance(val, datetime.date): return val
    return datetime.datetime.strptime(str(val)[:10], '%Y-%m-%d').date()

def month_label(d, today):
    label = d.strftime("%b %Y")
    return label + "*" if d >= datetime.date(today.year, today.month, 1) else label

def get_arr(rows, key, months, channel=None):
    lookup = {}
    for r in rows:
        m = to_date(r['month'])
        ch = str(r.get('channel', r.get('channelGrouping', '')))
        if channel is None or ch == channel:
            lookup[m] = r.get(key)
    return [lookup.get(m) for m in months]

def safe_div(a, b):
    return round(a/b, 4) if a and b else None

def daily(arr, days):
    return [round(arr[i]/days[i]) if arr[i] else None for i in range(len(arr))]

print("Loading current data.json...")
with open(DATA_JSON) as f:
    D = json.load(f)

print("Querying BigQuery...")
today = datetime.date.today()

# MAU
mau_rows = run("""
SELECT month,
  SUM(mau) as mau_app,
  SUM(CASE WHEN login_status='login' THEN mau END) as mau_login
FROM ct_product.dashboard__user_management_login_monthly
WHERE platform IN ('Android','iOS') AND month >= '2026-01-01'
GROUP BY 1 ORDER BY 1
""")

# DAU
dau_rows = run("""
SELECT DATE_TRUNC(date,MONTH) as month, AVG(daily_dau) as avg_dau
FROM (SELECT date, SUM(dau) as daily_dau
FROM ct_product.dashboard__user_management_DAU
WHERE platform IN ('Android','iOS') AND date >= '2026-01-01' GROUP BY date)
GROUP BY 1 ORDER BY 1
""")

# Total CT MAU
ct_rows = run("""
SELECT month, SUM(mau) as total_ct_mau
FROM ct_product.dashboard__user_management_login_monthly
WHERE month >= '2026-01-01' GROUP BY 1 ORDER BY 1
""")

# New users
new_rows = run("""
SELECT DATE_TRUNC(date,MONTH) as month, channelGrouping,
  COUNT(DISTINCT clientId) as new_mau,
  COUNT(DISTINCT CASE WHEN account_id IS NOT NULL THEN clientId END) as new_login_mau
FROM chotot_data.traffic_visit_detail
WHERE newVisits=1 AND platform IN ('iOS','Android') AND date >= '2026-01-01'
GROUP BY 1,2 ORDER BY 1,2
""")
print(f"  MAU: {len(mau_rows)} months | New users: {len(new_rows)} rows")

# Activation (may fail with 403)
act_rows = []
try:
    act_rows = run("""
    SELECT DATE_TRUNC(visit_date,MONTH) as month,
      CASE WHEN channel='all' THEN 'Total' ELSE channel END as channel,
      AVG(dau) as avg_new_dau,
      SUM(user_20adview_7d) as adview_total, SUM(user_1lead_7d) as lead_total,
      SAFE_DIVIDE(SUM(d1),SUM(d0)) as nurr_d1,
      SAFE_DIVIDE(SUM(d7),SUM(d0)) as nurr_d7,
      SAFE_DIVIDE(SUM(m1),SUM(d0)) as nurr_m1
    FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
    WHERE return_status='new' AND campaign='all' AND vertical_user='all'
    AND channel IN ('all','Direct','Organic Search','Paid Search','Display','Growth','Social')
    AND visit_date >= '2026-01-01' GROUP BY 1,2 ORDER BY 1,2
    """)
    print(f"  Activation: {len(act_rows)} rows OK")
except Exception as e:
    print(f"  WARNING Activation skipped: {e}")

# Retention (may fail with 403)
ret_rows = []
try:
    ret_rows = run("""
    SELECT DATE_TRUNC(min_date,MONTH) as month,
      SAFE_DIVIDE(SUM(d1),SUM(d0)) as ret_d1,
      SAFE_DIVIDE(SUM(d7),SUM(d0)) as ret_d7,
      SAFE_DIVIDE(SUM(m1),SUM(d0)) as ret_m1
    FROM ct_digital.dashboard__retention_90d
    WHERE new_status='return' AND platform IN ('iOS','Android') AND min_date >= '2026-01-01'
    GROUP BY 1 ORDER BY 1
    """)
    print(f"  Retention: {len(ret_rows)} rows OK")
except Exception as e:
    print(f"  WARNING Retention skipped: {e}")

# Build month list
all_months = sorted(set(to_date(r['month']) for r in mau_rows))
months_labels = [month_label(m, today) for m in all_months]
partial = [m for m in months_labels if m.endswith("*")]
n = len(all_months)

# ── Google Sheets cost sync ───────────────────────────────────────────────────
def fetch_sheet_cost(spreadsheet_id, months_list, current_month_idx):
    """Read FC & Actual cost sheet, extract app growth rows, return cost per month."""
    try:
        import gspread
        import google.auth
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(scopes=[
            'https://www.googleapis.com/auth/spreadsheets.readonly',
            'https://www.googleapis.com/auth/drive.readonly'
        ])
        creds.refresh(Request())

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet("FC & Actual cost")
        except Exception:
            ws = sh.get_worksheet(0)

        rows = ws.get_all_values()
        # Find header row with month names
        month_abbr = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        header_idx, col_map, act_col = 0, {}, 2
        for idx, row in enumerate(rows):
            hits = sum(1 for c in row if any(c.strip().startswith(m) for m in month_abbr))
            if hits >= 3:
                header_idx = idx
                for ci, cell in enumerate(row):
                    for mi, ma in enumerate(month_abbr):
                        if cell.strip().startswith(ma) and mi not in col_map:
                            col_map[mi] = ci
                    if 'activit' in cell.lower():
                        act_col = ci
                break

        actual_cost, forecast_cost = {}, {}
        for row in rows[header_idx+1:]:
            if len(row) <= act_col: continue
            if 'app growth' not in row[act_col].lower(): continue
            for mi, ci in col_map.items():
                if ci >= len(row): continue
                val_str = row[ci].replace(',','').replace(' ','').strip()
                if not val_str: continue
                try:
                    val = float(val_str)
                    if mi < current_month_idx:
                        actual_cost[mi] = actual_cost.get(mi, 0) + val
                    else:
                        forecast_cost[mi] = forecast_cost.get(mi, 0) + val
                except ValueError:
                    pass

        def get_mi(lbl):
            for i, ma in enumerate(month_abbr):
                if lbl.startswith(ma): return i
            return -1

        cost_out, fc_out = [], []
        for lbl in months_list:
            mi = get_mi(lbl)
            if mi < 0:
                cost_out.append(None); fc_out.append(None)
            elif mi < current_month_idx:
                cost_out.append(int(actual_cost.get(mi,0)) or None); fc_out.append(None)
            else:
                cost_out.append(None); fc_out.append(int(forecast_cost.get(mi,0)) or None)

        print(f"  Sheet: {sum(1 for c in cost_out if c)} actual months, {sum(1 for c in fc_out if c)} forecast months")
        return cost_out, [v for v in fc_out if v]
    except Exception as e:
        print(f"  WARNING Sheet fetch failed: {e}")
        return None, None



# Days per month
def days_in(d):
    if d.month == 12: return 31
    return (datetime.date(d.year, d.month+1, 1) - datetime.timedelta(days=1)).day
days = [days_in(m) for m in all_months]

# Overview
mau_app   = get_arr(mau_rows, 'mau_app', all_months)
mau_login = get_arr(mau_rows, 'mau_login', all_months)
ct_mau    = get_arr(ct_rows, 'total_ct_mau', all_months)
avg_dau   = [round(v) if v else None for v in get_arr(dau_rows, 'avg_dau', all_months)]

# New users aggregated
ch_map = {}
for r in new_rows:
    m = to_date(r['month'])
    ch = r.get('channelGrouping','')
    ch_map[(m,ch)] = r

def by_ch(ch, key='new_mau'):
    return [ch_map.get((m,ch),{}).get(key, 0) or 0 for m in all_months]

direct_n  = by_ch('Direct'); organic_n = by_ch('Organic Search')
paid_n    = by_ch('Paid Search'); display_n= by_ch('Display')
growth_crm= by_ch('Growth'); other_n   = by_ch('(Other)')
growth_n  = [paid_n[i]+display_n[i]+growth_crm[i] for i in range(n)]
total_n   = [direct_n[i]+organic_n[i]+growth_n[i]+other_n[i] for i in range(n)]
new_login_total = [
    by_ch('Direct','new_login_mau')[i] + by_ch('Organic Search','new_login_mau')[i] +
    by_ch('Paid Search','new_login_mau')[i] + by_ch('Display','new_login_mau')[i] +
    by_ch('Growth','new_login_mau')[i] + by_ch('(Other)','new_login_mau')[i]
    for i in range(n)]

# Activation / NURR
if act_rows:
    def a(ch, key): return get_arr(act_rows, key, all_months, channel=ch)
    adview_total = a('Total','adview_total'); lead_total = a('Total','lead_total')
    nurr_d1=a('Total','nurr_d1'); nurr_d7=a('Total','nurr_d7'); nurr_m1=a('Total','nurr_m1')
    dir_adv=a('Direct','adview_total'); org_adv=a('Organic Search','adview_total')
    paid_adv=a('Paid Search','adview_total'); disp_adv=a('Display','adview_total')
    crm_adv=a('Growth','adview_total')
    growth_adv=[( paid_adv[i] or 0)+(disp_adv[i] or 0)+(crm_adv[i] or 0) for i in range(n)]
    dir_lead=a('Direct','lead_total'); org_lead=a('Organic Search','lead_total')
    paid_lead=a('Paid Search','lead_total'); disp_lead=a('Display','lead_total')
    crm_lead=a('Growth','lead_total')
    growth_lead=[(paid_lead[i] or 0)+(disp_lead[i] or 0)+(crm_lead[i] or 0) for i in range(n)]
    dir_d1=a('Direct','nurr_d1'); dir_d7=a('Direct','nurr_d7'); dir_m1=a('Direct','nurr_m1')
    org_d1=a('Organic Search','nurr_d1'); org_d7=a('Organic Search','nurr_d7'); org_m1=a('Organic Search','nurr_m1')
    paid_d1=a('Paid Search','nurr_d1'); paid_d7=a('Paid Search','nurr_d7'); paid_m1=a('all','nurr_m1')  # M1: use Total channel (M1 from Paid Search is unreliable)
else:
    print("  Using existing activation data")
    ex=D['activation']; er=D['retention']; eg=D['growth_channel']
    adview_total=ex['adview_total']; lead_total=ex['lead_total']
    nurr_d1=er['nurr_d1']; nurr_d7=er['nurr_d7']; nurr_m1=er['nurr_m1']
    dir_adv=ex['direct_adview']; org_adv=ex['organic_adview']; growth_adv=ex['growth_adview']
    dir_lead=ex['direct_lead']; org_lead=ex['organic_lead']; growth_lead=ex['growth_lead']
    dir_d1=er['direct_d1']; dir_d7=er['direct_d7']; dir_m1=er['direct_m1']
    org_d1=er['organic_d1']; org_d7=er['organic_d7']; org_m1=er['organic_m1']
    paid_d1=eg['nurr_d1']; paid_d7=eg['nurr_d7']; paid_m1=eg['nurr_m1']

if ret_rows:
    app_d1=get_arr(ret_rows,'ret_d1',all_months); app_d7=get_arr(ret_rows,'ret_d7',all_months)
    app_m1=get_arr(ret_rows,'ret_m1',all_months)
else:
    er=D['retention']
    app_d1=er['app_d1']; app_d7=er['app_d7']; app_m1=er['app_m1']

def pad(arr, length, val=None):
    return list(arr) + [val]*(length-len(arr))

tot_d1=pad(D['retention']['total_d1'],n); tot_d7=pad(D['retention']['total_d7'],n)
tot_m1=pad(D['retention']['total_m1'],n)

# Cost — sync from Google Sheets
SHEET_ID = '1D-2eQcfDMzy42wHUF4bpwCY4cWtrJNvp-kdv9R_iFUI'
current_month_idx = today.month - 1
sheet_actual, sheet_forecast = fetch_sheet_cost(SHEET_ID, months_labels, current_month_idx)

if sheet_actual:
    cost = sheet_actual  # actual costs per month (None for future months)
    # Merge: use sheet data where available, keep existing for gaps
    existing_cost = D['growth_channel']['cost']
    for i in range(n):
        if cost[i] is None and i < len(existing_cost) and existing_cost[i]:
            cost[i] = existing_cost[i]  # keep existing actual if sheet missing
else:
    cost = pad(D['growth_channel']['cost'], n)

# Forecast cost (Jun-Dec planning)
if sheet_forecast:
    new_forecast = [v for v in sheet_forecast if v]
else:
    new_forecast = D['growth_channel'].get('cost_forecast', [])
gc_new = growth_n
ret_d1_gc=[round(gc_new[i]*(paid_d1[i] or 0)) if gc_new[i] else None for i in range(n)]
ret_d7_gc=[round(gc_new[i]*(paid_d7[i] or 0)) if gc_new[i] else None for i in range(n)]
ret_m1_gc=[round(gc_new[i]*(paid_m1[i] or 0)) if gc_new[i] and paid_m1[i] else None for i in range(n)]

# Build output
out = {
    "updated_at": today.strftime("%Y-%m-%d"),
    "months": months_labels,
    "partial_months": partial,
    "overview": {
        "mau_app": mau_app, "mau_login": mau_login,
        "mau_nonlogin": [a-b if a and b else None for a,b in zip(mau_app,mau_login)],
        "avg_dau": avg_dau, "total_ct_mau": ct_mau,
        "web_other_mau": [a-b if a and b else None for a,b in zip(ct_mau,mau_app)],
        "new_mau": total_n, "new_login_mau": new_login_total,
        "avg_new_dau": daily(total_n, days),
        "returning_mau": [a-b if a and b else None for a,b in zip(mau_app,total_n)],
        "pct_new": [safe_div(total_n[i],mau_app[i]) for i in range(n)],
        "login_rate": [safe_div(mau_login[i],mau_app[i]) for i in range(n)],
        "new_login_rate": [safe_div(new_login_total[i],total_n[i]) for i in range(n)],
        "pct_app_ct": [safe_div(mau_app[i],ct_mau[i]) for i in range(n)],
    },
    "acquisition": {
        "direct": direct_n, "organic": organic_n, "growth": gc_new, "other": other_n,
        "direct_daily": daily(direct_n,days), "organic_daily": daily(organic_n,days),
        "growth_daily": daily(gc_new,days), "other_daily": daily(other_n,days),
        "growth_pct_total": [safe_div(gc_new[i],total_n[i]) for i in range(n)],
    },
    "activation": {
        "adview_total": adview_total, "lead_total": lead_total,
        "adview_rate": [safe_div(adview_total[i],total_n[i]) for i in range(n)],
        "lead_rate": [safe_div(lead_total[i],total_n[i]) for i in range(n)],
        "adview_daily": daily(adview_total,days), "lead_daily": daily(lead_total,days),
        "direct_adview": dir_adv, "organic_adview": org_adv, "growth_adview": growth_adv,
        "direct_lead": dir_lead, "organic_lead": org_lead, "growth_lead": growth_lead,
        "direct_adview_daily": daily(dir_adv,days), "organic_adview_daily": daily(org_adv,days),
        "growth_adview_daily": daily(growth_adv,days),
        "direct_lead_daily": daily(dir_lead,days), "organic_lead_daily": daily(org_lead,days),
        "growth_lead_daily": daily(growth_lead,days),
    },
    "retention": {
        "total_d1": tot_d1, "total_d7": tot_d7, "total_m1": tot_m1,
        "app_d1": app_d1, "app_d7": app_d7, "app_m1": app_m1,
        "nurr_d1": nurr_d1, "nurr_d7": nurr_d7, "nurr_m1": nurr_m1,
        "direct_d1": dir_d1, "direct_d7": dir_d7, "direct_m1": dir_m1,
        "organic_d1": org_d1, "organic_d7": org_d7, "organic_m1": org_m1,
        "growth_d1": paid_d1, "growth_d7": paid_d7, "growth_m1": paid_m1,
    },
    "growth_channel": {
        "new_users": gc_new, "avg_new_dau": daily(gc_new,days),
        "adview_activated": growth_adv, "lead_activated": growth_lead,
        "adview_rate": [safe_div(growth_adv[i],gc_new[i]) for i in range(n)],
        "lead_rate": [safe_div(growth_lead[i],gc_new[i]) for i in range(n)],
        "adview_daily": daily(growth_adv,days), "lead_daily": daily(growth_lead,days),
        "nurr_d1": paid_d1, "nurr_d7": paid_d7, "nurr_m1": paid_m1,
        "cost": cost, "cost_forecast": new_forecast,
        "pct_of_total_new": [safe_div(gc_new[i],total_n[i]) for i in range(n)],
        "retained_d1": ret_d1_gc, "retained_d7": ret_d7_gc, "retained_m1": ret_m1_gc,
        "cpa": [round(cost[i]/gc_new[i]) if cost[i] and gc_new[i] else None for i in range(n)],
        "caa": [round(cost[i]/growth_adv[i]) if cost[i] and growth_adv[i] else None for i in range(n)],
        "crr_d1": [round(cost[i]/ret_d1_gc[i]) if cost[i] and ret_d1_gc[i] else None for i in range(n)],
        "crr_d7": [round(cost[i]/ret_d7_gc[i]) if cost[i] and ret_d7_gc[i] else None for i in range(n)],
        "crr_m1": [round(cost[i]/ret_m1_gc[i]) if cost[i] and ret_m1_gc[i] else None for i in range(n)],
    }
}

with open(DATA_JSON, 'w') as f:
    json.dump(out, f, indent=2, default=str)

print(f"✅ data.json updated — {months_labels}")
print(f"   Latest: {months_labels[-1]} | App MAU: {mau_app[-1]:,} | New: {total_n[-1]:,}")
