"""
Chotot Marketplace Dashboard — Auto Update Script
Queries BigQuery chotot_mtm + Google Sheets cost data.
No Claude/Anthropic API. $0 token cost.
Run: python3 scripts/update_marketplace.py
"""
import re, os, sys, datetime, subprocess, urllib.request, json
import google.auth
from google.cloud import bigquery

sys.path.insert(0, os.path.dirname(__file__))
from url_tracking_classifier import classify_cluster, pty_campaign_group, job_campaign_group

PROJECT   = "chotot-dwh"
SRC_HTML  = os.path.join(os.path.dirname(__file__), '..', 'src', 'index.html')
DIST_HTML = os.path.join(os.path.dirname(__file__), '..', 'dist', 'index.html')
ROOT_HTML = os.path.join(os.path.dirname(__file__), '..', 'index.html')

# BQ client with Drive scope so Sheets-linked tables (kiet_gg_ad_cost, kiet_url_tracking_pty/_job)
# are accessible.
_SCOPES = [
    'https://www.googleapis.com/auth/bigquery',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/cloud-platform',
]
_creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
if _creds_path:
    # load_credentials_from_file auto-detects the JSON's credential type (service_account vs
    # authorized_user) instead of assuming service_account — GCP_USER_CREDENTIALS is a personal
    # OAuth "authorized_user" token (from `gcloud auth application-default login`), not a
    # service-account key (see O-060 in chotot-digital repo for the failure this caused).
    _creds, _ = google.auth.load_credentials_from_file(_creds_path, scopes=_SCOPES)
    client = bigquery.Client(project=PROJECT, credentials=_creds)
else:
    _creds = None
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

# ── 3d. URL Tracking (PTY/JOB Google Ads landing-page focus tracking) ───────
# Reads via the kiet_url_tracking_pty/_job BigQuery external tables (GOOGLE_SHEETS format, same
# live-pointer mechanism as kiet_gg_ad_cost) instead of calling the Sheets API directly — the
# direct Sheets API call hit USER_PROJECT_DENIED (no serviceusage.serviceUsageConsumer on this
# project) which BigQuery's own external-table read doesn't need. See O-063 in chotot-digital repo.
URL_TRACKING_TABLES = {'PTY': 'kiet_url_tracking_pty', 'JOB': 'kiet_url_tracking_job'}
URL_TRACKING_MAX_DAYS = 30  # chart window cap; campaign tables/KPIs use all available rows


def fetch_url_tracking_rows(vertical):
    """Read the PTY-URL/JOB-URL tab (append-only, growing forever) via its BigQuery external
    table. Returns a list of dicts keyed by column name. Raises on query failure — callers must
    NOT silently substitute fake data on error, per this task's explicit instruction."""
    table = URL_TRACKING_TABLES[vertical]
    return q(f"""
        SELECT date, campaign_id, campaign_name, campaign_status, channel_type,
               ad_network_type, landing_page_url, impressions, clicks, cost_vnd
        FROM `{PROJECT}.ct_digital.{table}`
    """)


def _parse_num(v):
    if v is None:
        return 0.0
    s = str(v).strip().replace(',', '').replace('₫', '')
    if s in ('', '-', '--'):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


_CLUSTER_KEY = {'Focus': 'focus', 'Generic': 'generic', 'Non-focus': 'nonfocus'}


def build_vertical_url_tracking(vertical, rows):
    """Classify every row (date, campaign, URL, clicks, cost) into the Focus/Generic/Non-focus
    3-way cluster (D-017), then aggregate into: daily cluster totals (for the trend chart),
    campaign-level non-focus breakdown grouped by Let/Sell (PTY) or job type (JOB), and — PTY
    only — the price-range bonus-signal coverage of Focus traffic."""
    totals = {'cost': 0.0, 'clicks': 0.0, 'focusCost': 0.0, 'focusClicks': 0.0,
              'genericCost': 0.0, 'genericClicks': 0.0, 'nonfocusCost': 0.0, 'nonfocusClicks': 0.0}
    daily = {}  # date -> metrics dict
    camp = {}   # campaign_name -> {cost, clicks, nonfocus_cost, nonfocus_clicks, reasons:{label:{cost,clicks}}}
    combo = {}  # "{segment} x {location}" -> {cost, clicks, eligible} -- the Location x Sub-category
                # breakdown (mirrors the vertical's own "PTY-JOB URL Audit" Google Sheet), distinct
                # from the collapsed 3-way cluster above
    price_focus_cost = price_focus_cost_priced = 0.0
    price_focus_clicks = price_focus_clicks_priced = 0.0
    skipped = 0

    for r in rows:
        date = (r.get('date') or '').strip()
        url = (r.get('landing_page_url') or '').strip()
        campaign_name = (r.get('campaign_name') or '').strip()
        if not date or not url:
            skipped += 1
            continue
        clicks = _parse_num(r.get('clicks'))
        cost = _parse_num(r.get('cost_vnd'))

        cls = classify_cluster(vertical, url)
        cluster = cls['cluster']
        ck = _CLUSTER_KEY[cluster]

        totals['cost'] += cost
        totals['clicks'] += clicks
        totals[f'{ck}Cost'] += cost
        totals[f'{ck}Clicks'] += clicks

        d = daily.setdefault(date, {'date': date, 'cost': 0.0, 'clicks': 0.0,
                                     'focusCost': 0.0, 'focusClicks': 0.0,
                                     'genericCost': 0.0, 'genericClicks': 0.0,
                                     'nonfocusCost': 0.0, 'nonfocusClicks': 0.0})
        d['cost'] += cost
        d['clicks'] += clicks
        d[f'{ck}Cost'] += cost
        d[f'{ck}Clicks'] += clicks

        if vertical == 'PTY' and cluster == 'Focus':
            price_focus_cost += cost
            price_focus_clicks += clicks
            if cls['price_signal']:
                price_focus_cost_priced += cost
                price_focus_clicks_priced += clicks

        # cluster is deterministic for a given (segment, location) pair, so it's safe to set once
        # per combined_label rather than re-derive on every row.
        cc = combo.setdefault(cls['combined_label'], {'cost': 0.0, 'clicks': 0.0, 'cluster': cluster})
        cc['cost'] += cost
        cc['clicks'] += clicks

        if campaign_name:
            c = camp.setdefault(campaign_name, {'cost': 0.0, 'clicks': 0.0,
                                                 'nonfocus_cost': 0.0, 'nonfocus_clicks': 0.0,
                                                 'reasons': {}})
            c['cost'] += cost
            c['clicks'] += clicks
            # Campaign-table "Non-focus" is the single Non-focus cluster ONLY (not combined with
            # Generic) — the KPI card above is deliberately named "Leaking" instead of "Non-focus"
            # specifically to avoid this column meaning something different from that card.
            if cluster == 'Non-focus':
                c['nonfocus_cost'] += cost
                c['nonfocus_clicks'] += clicks
                reason = c['reasons'].setdefault(cls['combined_label'], {'cost': 0.0, 'clicks': 0.0})
                reason['cost'] += cost
                reason['clicks'] += clicks

    # Group campaigns (Let/Sell for PTY; 8 job types + Generic/multiple for JOB), omit empty groups.
    groups = {}
    group_fn = pty_campaign_group if vertical == 'PTY' else job_campaign_group
    for name, c in camp.items():
        group_name = group_fn(name)
        top_reason = max(c['reasons'].items(), key=lambda kv: kv[1]['cost'], default=(None, None))
        top_cluster = top_reason[0].replace(' x ', ' × ') if top_reason[0] else '—'
        g = groups.setdefault(group_name, {'name': group_name, 'cost': 0.0, 'clicks': 0.0,
                                            'nonfocusCost': 0.0, 'nonfocusClicks': 0.0, 'campaigns': []})
        g['cost'] += c['cost']
        g['clicks'] += c['clicks']
        g['nonfocusCost'] += c['nonfocus_cost']
        g['nonfocusClicks'] += c['nonfocus_clicks']
        g['campaigns'].append({
            'name': name, 'cost': round(c['cost']), 'clicks': round(c['clicks']),
            'nonfocusCost': round(c['nonfocus_cost']), 'nonfocusClicks': round(c['nonfocus_clicks']),
            'topCluster': top_cluster,
        })

    for g in groups.values():
        g['campaigns'].sort(key=lambda x: -x['nonfocusCost'])
        g['cost'] = round(g['cost']); g['clicks'] = round(g['clicks'])
        g['nonfocusCost'] = round(g['nonfocusCost']); g['nonfocusClicks'] = round(g['nonfocusClicks'])
    group_list = sorted(groups.values(), key=lambda g: -g['nonfocusCost'])

    daily_list = sorted(daily.values(), key=lambda d: d['date'])[-URL_TRACKING_MAX_DAYS:]
    for d in daily_list:
        for k in list(d.keys()):
            if k != 'date':
                d[k] = round(d[k])
    for k in totals:
        totals[k] = round(totals[k])

    # Location x Sub-category breakdown -- top N combinations by cost get their own row, the long
    # tail rolls into one "All other combinations" line, same pattern as the vertical's audit sheet.
    COMBO_TOP_N = 30
    combo_sorted = sorted(combo.items(), key=lambda kv: -kv[1]['cost'])
    combo_top, combo_tail = combo_sorted[:COMBO_TOP_N], combo_sorted[COMBO_TOP_N:]
    combos = [{'label': label, 'cost': round(v['cost']), 'clicks': round(v['clicks']), 'cluster': v['cluster']}
              for label, v in combo_top]
    combo_rest = {
        'n': len(combo_tail),
        'cost': round(sum(v['cost'] for _, v in combo_tail)),
        'clicks': round(sum(v['clicks'] for _, v in combo_tail)),
    }

    result = {'totals': totals, 'daily': daily_list, 'groups': group_list,
              'combos': combos, 'comboRest': combo_rest}
    if vertical == 'PTY':
        result['priceTag'] = {
            'focusCost': round(price_focus_cost), 'focusCostPriced': round(price_focus_cost_priced),
            'focusClicks': round(price_focus_clicks), 'focusClicksPriced': round(price_focus_clicks_priced),
        }
    else:
        result['priceTag'] = None
    result['_skipped_rows'] = skipped
    result['_total_rows'] = len(rows)
    return result


def build_url_tracking():
    """Fetch + classify both PTY-URL/JOB-URL tabs. Prints a summary (row counts, cluster %) so
    there's a paper trail. Returns None (leaving the existing embedded data untouched) if the
    Sheets read fails — never substitutes fake/placeholder data on error."""
    try:
        pty_rows = fetch_url_tracking_rows('PTY')
        job_rows = fetch_url_tracking_rows('JOB')
    except Exception as e:
        print(f"  ❌ URL Tracking Sheets read FAILED: {type(e).__name__}: {e}")
        return None

    pty = build_vertical_url_tracking('PTY', pty_rows)
    job = build_vertical_url_tracking('JOB', job_rows)

    all_dates = [d['date'] for d in pty['daily']] + [d['date'] for d in job['daily']]
    data_as_of = max(all_dates) if all_dates else None

    def pct(cost, total):
        return round(cost / total * 100, 1) if total else 0.0

    print(f"  PTY-URL: {pty['_total_rows']} rows ({pty['_skipped_rows']} skipped) — "
          f"Focus {pct(pty['totals']['focusCost'], pty['totals']['cost'])}% / "
          f"Generic {pct(pty['totals']['genericCost'], pty['totals']['cost'])}% / "
          f"Non-focus {pct(pty['totals']['nonfocusCost'], pty['totals']['cost'])}% of cost")
    print(f"  JOB-URL: {job['_total_rows']} rows ({job['_skipped_rows']} skipped) — "
          f"Focus {pct(job['totals']['focusCost'], job['totals']['cost'])}% / "
          f"Generic {pct(job['totals']['genericCost'], job['totals']['cost'])}% / "
          f"Non-focus {pct(job['totals']['nonfocusCost'], job['totals']['cost'])}% of cost")
    if pty['priceTag']:
        pt = pty['priceTag']
        print(f"  PTY price-tag coverage of Focus cost: {pct(pt['focusCostPriced'], pt['focusCost'])}%")
    print(f"  data_as_of = {data_as_of}")

    pty.pop('_skipped_rows', None); pty.pop('_total_rows', None)
    job.pop('_skipped_rows', None); job.pop('_total_rows', None)
    return {'dataAsOf': data_as_of, 'PTY': pty, 'JOB': job}


print("Fetching URL Tracking sheet data (PTY-URL / JOB-URL)...")
url_tracking = build_url_tracking()

# ── 3e. Segment Mix (PTY: ad_type x category x region x price tier; JOB: job_type x region) ─
# Direct BigQuery, no Sheets — unlike URL Tracking this data never lived in a spreadsheet.
# Deliberately does NOT sync the vertical's own live demand-supply quadrant label (that table
# only has ~2 weeks populated at a time — see chotot-digital's O-039); tracks our own universal
# attributes instead. Full design rationale: chotot-digital repo, context/decisions.md D-020 and
# tools/queries/pty|job/digital-lead-segment-mix.sql. Rolling 30-day window, computed in SQL
# (CURRENT_DATE()), so no Python date interpolation needed here.
#
# ⚠️ TWO QUERIES PER VERTICAL, ON PURPOSE — corrected 2026-08-18 after Kiet caught an implausibly
# large DWL number in production (548,247 DWL for one PTY bucket over 30 days). Root cause: the
# first version computed DWL per ad_id, then SUMMED those per-ad values into each bucket — which
# double/many-times counts anyone who contacts more than one ad within the same bucket on the same
# day (routine — e.g. a room-hunter messaging several Room-HCM-low listings). Plain counts
# (adview/lead) are safely summable across any regrouping; COUNT(DISTINCT ...) is NOT. Fix: one
# query returns bucket-level adview_total/lead_total (plain counts, aggregate freely in Python);
# a second returns raw DISTINCT (date, clientId, bucket-dims) rows that Python groups and
# COUNT(DISTINCT date||clientId)'s itself at each grain shown (tier-only, category-only,
# region-only, full detail, grand total) — never by summing a finer grain's distinct count. See
# the DWL hard check in chotot-digital's context/shared/measurement/warehouse-model.md.
_PTY_BUCKET_SQL = """
WITH date_refs AS (
  SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AS window_end,
         DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AS window_start
),
ad_w_info AS (
  SELECT
    a1.ad_id, a1.ad_type,
    CASE WHEN a1.category = 1010 THEN 'Apartments' WHEN a1.category = 1020 THEN 'Houses'
         WHEN a1.category = 1030 THEN 'Offices' WHEN a1.category = 1040 THEN 'Land'
         WHEN a1.category = 1050 THEN 'Rooms' END AS category_name,
    CASE WHEN a1.city_name = 'Tp Hồ Chí Minh' THEN 'HCM' WHEN a1.city_name = 'Bình Dương' THEN 'BD'
         WHEN a1.city_name = 'Hà Nội' THEN 'HN' WHEN a1.city_name = 'Đà Nẵng' THEN 'DN'
         ELSE 'other' END AS region,
    CASE
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2026 AND a1.city_name IN ('Hà Nội')
        THEN CASE WHEN a1.price < (t.mid_tier*1.3225) THEN 'low' WHEN a1.price < (t.high_tier*1.3225) THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2026 AND a1.city_name NOT IN ('Hà Nội')
        THEN CASE WHEN a1.price < (t.mid_tier*1.265) THEN 'low' WHEN a1.price < (t.high_tier*1.265) THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2025
        THEN CASE WHEN a1.price < (t.mid_tier*1.10) THEN 'low' WHEN a1.price < (t.high_tier*1.10) THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2024
        THEN CASE WHEN a1.price < t.mid_tier THEN 'low' WHEN a1.price < t.high_tier THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) < 2024
        THEN CASE WHEN a1.price < n.price_segment THEN 'low' ELSE 'high_mid' END
    END AS tier
  FROM `chotot-dwh.chotot_data.ad` a1
  LEFT JOIN `chotot-dwh.ct_nha.tuan_price_segment` t ON a1.city_id = t.city AND a1.category = t.category AND a1.ad_type = t.ad_type
  LEFT JOIN `chotot-dwh.ct_nha.nguyen_new_market_segmentation_2025` n ON a1.city_name = n.city_name AND a1.ad_type = n.ad_type AND a1.category = n.category
  WHERE a1.dwh_update_time >= '2022-01-01' AND a1.first_approved_time IS NOT NULL AND a1.category BETWEEN 1000 AND 1050
),
digital_visits AS (
  SELECT DISTINCT v.date, v.clientId, CAST(v.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_visit_detail` v CROSS JOIN date_refs dr
  WHERE v.date BETWEEN dr.window_start AND dr.window_end
    AND v.is_bot IS NULL
    AND v.channelGrouping IN ('Paid Search','Display')
    AND v.source IN ('google','google_search','facebook')
    AND LOWER(v.campaign) NOT LIKE '%appinstall%'
    AND LOWER(v.campaign) NOT LIKE '%install_app%'
)
SELECT
  COALESCE(a.ad_type, l.ad_type) AS ad_type,
  COALESCE(a.category_name, l.category_name) AS category_name,
  COALESCE(a.region, l.region) AS region,
  COALESCE(a.tier, l.tier) AS tier,
  COALESCE(a.adview_total, 0) AS adview_total,
  COALESCE(l.lead_total, 0) AS lead_total
FROM (
  SELECT w.ad_type, w.category_name, w.region, w.tier, COUNT(*) AS adview_total
  FROM `chotot-dwh.chotot_data.traffic_pageview_detail` p CROSS JOIN date_refs dr
  INNER JOIN digital_visits dv ON p.date = dv.date AND p.clientId = dv.clientId AND CAST(p.visitId AS STRING) = dv.visitId
  INNER JOIN ad_w_info w ON p.ad_id = w.ad_id
  WHERE p.date BETWEEN dr.window_start AND dr.window_end
    AND p.page_type IN ('adview','ad_view') AND p.ad_id IS NOT NULL
  GROUP BY 1,2,3,4
) a
FULL OUTER JOIN (
  SELECT w.ad_type, w.category_name, w.region, w.tier, COUNT(*) AS lead_total
  FROM `chotot-dwh.chotot_data.traffic_lead_detail` l CROSS JOIN date_refs dr
  INNER JOIN digital_visits dv ON l.date = dv.date AND l.clientId = dv.clientId AND CAST(l.visitId AS STRING) = dv.visitId
  INNER JOIN ad_w_info w ON l.ad_id = w.ad_id
  WHERE l.date BETWEEN dr.window_start AND dr.window_end AND l.is_bot IS NULL AND l.ad_id IS NOT NULL
  GROUP BY 1,2,3,4
) l
ON a.ad_type = l.ad_type AND a.category_name = l.category_name AND a.region = l.region AND a.tier = l.tier
"""

_PTY_DWL_RAW_SQL = """
WITH date_refs AS (
  SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AS window_end,
         DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AS window_start
),
ad_w_info AS (
  SELECT
    a1.ad_id, a1.ad_type,
    CASE WHEN a1.category = 1010 THEN 'Apartments' WHEN a1.category = 1020 THEN 'Houses'
         WHEN a1.category = 1030 THEN 'Offices' WHEN a1.category = 1040 THEN 'Land'
         WHEN a1.category = 1050 THEN 'Rooms' END AS category_name,
    CASE WHEN a1.city_name = 'Tp Hồ Chí Minh' THEN 'HCM' WHEN a1.city_name = 'Bình Dương' THEN 'BD'
         WHEN a1.city_name = 'Hà Nội' THEN 'HN' WHEN a1.city_name = 'Đà Nẵng' THEN 'DN'
         ELSE 'other' END AS region,
    CASE
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2026 AND a1.city_name IN ('Hà Nội')
        THEN CASE WHEN a1.price < (t.mid_tier*1.3225) THEN 'low' WHEN a1.price < (t.high_tier*1.3225) THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2026 AND a1.city_name NOT IN ('Hà Nội')
        THEN CASE WHEN a1.price < (t.mid_tier*1.265) THEN 'low' WHEN a1.price < (t.high_tier*1.265) THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2025
        THEN CASE WHEN a1.price < (t.mid_tier*1.10) THEN 'low' WHEN a1.price < (t.high_tier*1.10) THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) = 2024
        THEN CASE WHEN a1.price < t.mid_tier THEN 'low' WHEN a1.price < t.high_tier THEN 'mid' ELSE 'high' END
      WHEN EXTRACT(YEAR FROM a1.first_approved_time) < 2024
        THEN CASE WHEN a1.price < n.price_segment THEN 'low' ELSE 'high_mid' END
    END AS tier
  FROM `chotot-dwh.chotot_data.ad` a1
  LEFT JOIN `chotot-dwh.ct_nha.tuan_price_segment` t ON a1.city_id = t.city AND a1.category = t.category AND a1.ad_type = t.ad_type
  LEFT JOIN `chotot-dwh.ct_nha.nguyen_new_market_segmentation_2025` n ON a1.city_name = n.city_name AND a1.ad_type = n.ad_type AND a1.category = n.category
  WHERE a1.dwh_update_time >= '2022-01-01' AND a1.first_approved_time IS NOT NULL AND a1.category BETWEEN 1000 AND 1050
),
digital_visits AS (
  SELECT DISTINCT v.date, v.clientId, CAST(v.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_visit_detail` v CROSS JOIN date_refs dr
  WHERE v.date BETWEEN dr.window_start AND dr.window_end
    AND v.is_bot IS NULL
    AND v.channelGrouping IN ('Paid Search','Display')
    AND v.source IN ('google','google_search','facebook')
    AND LOWER(v.campaign) NOT LIKE '%appinstall%'
    AND LOWER(v.campaign) NOT LIKE '%install_app%'
)
SELECT DISTINCT
  l.date, l.clientId, w.ad_type, w.category_name, w.region, w.tier
FROM `chotot-dwh.chotot_data.traffic_lead_detail` l CROSS JOIN date_refs dr
INNER JOIN digital_visits dv ON l.date = dv.date AND l.clientId = dv.clientId AND CAST(l.visitId AS STRING) = dv.visitId
INNER JOIN ad_w_info w ON l.ad_id = w.ad_id
WHERE l.date BETWEEN dr.window_start AND dr.window_end AND l.is_bot IS NULL AND l.ad_id IS NOT NULL
"""

_JOB_BUCKET_SQL = """
WITH date_refs AS (
  SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AS window_end,
         DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AS window_start
),
job_ad_info AS (
  SELECT a.ad_id, p.value_name AS job_type,
    CASE WHEN a.city_name = 'Tp Hồ Chí Minh' THEN 'HCM' WHEN a.city_name = 'Bình Dương' THEN 'BD'
         WHEN a.city_name = 'Hà Nội' THEN 'HN' WHEN a.city_name = 'Đà Nẵng' THEN 'DN'
         ELSE 'other' END AS region
  FROM `chotot-dwh.chotot_data.ad` a
  LEFT JOIN UNNEST(a.params) p ON p.name = 'job_type'
  WHERE a.vertical = 'JOBS' AND a.dwh_update_time >= '2022-01-01'
),
digital_visits AS (
  SELECT DISTINCT v.date, v.clientId, CAST(v.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_visit_detail` v CROSS JOIN date_refs dr
  WHERE v.date BETWEEN dr.window_start AND dr.window_end
    AND v.is_bot IS NULL
    AND v.channelGrouping IN ('Paid Search','Display')
    AND v.source IN ('google','google_search','facebook')
    AND LOWER(v.campaign) NOT LIKE '%appinstall%'
    AND LOWER(v.campaign) NOT LIKE '%install_app%'
)
SELECT
  COALESCE(a.job_type, l.job_type) AS job_type,
  COALESCE(a.region, l.region) AS region,
  COALESCE(a.adview_total, 0) AS adview_total,
  COALESCE(l.lead_total, 0) AS lead_total
FROM (
  SELECT j.job_type, j.region, COUNT(*) AS adview_total
  FROM `chotot-dwh.chotot_data.traffic_pageview_detail` p CROSS JOIN date_refs dr
  INNER JOIN digital_visits dv ON p.date = dv.date AND p.clientId = dv.clientId AND CAST(p.visitId AS STRING) = dv.visitId
  INNER JOIN job_ad_info j ON p.ad_id = j.ad_id
  WHERE p.date BETWEEN dr.window_start AND dr.window_end
    AND p.page_type IN ('adview','ad_view') AND p.ad_id IS NOT NULL
  GROUP BY 1,2
) a
FULL OUTER JOIN (
  SELECT j.job_type, j.region, COUNT(*) AS lead_total
  FROM `chotot-dwh.chotot_data.traffic_lead_detail` l CROSS JOIN date_refs dr
  INNER JOIN digital_visits dv ON l.date = dv.date AND l.clientId = dv.clientId AND CAST(l.visitId AS STRING) = dv.visitId
  INNER JOIN job_ad_info j ON l.ad_id = j.ad_id
  WHERE l.date BETWEEN dr.window_start AND dr.window_end AND l.is_bot IS NULL AND l.ad_id IS NOT NULL
  GROUP BY 1,2
) l
ON a.job_type = l.job_type AND a.region = l.region
"""

_JOB_DWL_RAW_SQL = """
WITH date_refs AS (
  SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AS window_end,
         DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AS window_start
),
job_ad_info AS (
  SELECT a.ad_id, p.value_name AS job_type,
    CASE WHEN a.city_name = 'Tp Hồ Chí Minh' THEN 'HCM' WHEN a.city_name = 'Bình Dương' THEN 'BD'
         WHEN a.city_name = 'Hà Nội' THEN 'HN' WHEN a.city_name = 'Đà Nẵng' THEN 'DN'
         ELSE 'other' END AS region
  FROM `chotot-dwh.chotot_data.ad` a
  LEFT JOIN UNNEST(a.params) p ON p.name = 'job_type'
  WHERE a.vertical = 'JOBS' AND a.dwh_update_time >= '2022-01-01'
),
digital_visits AS (
  SELECT DISTINCT v.date, v.clientId, CAST(v.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_visit_detail` v CROSS JOIN date_refs dr
  WHERE v.date BETWEEN dr.window_start AND dr.window_end
    AND v.is_bot IS NULL
    AND v.channelGrouping IN ('Paid Search','Display')
    AND v.source IN ('google','google_search','facebook')
    AND LOWER(v.campaign) NOT LIKE '%appinstall%'
    AND LOWER(v.campaign) NOT LIKE '%install_app%'
)
SELECT DISTINCT
  l.date, l.clientId, j.job_type, j.region
FROM `chotot-dwh.chotot_data.traffic_lead_detail` l CROSS JOIN date_refs dr
INNER JOIN digital_visits dv ON l.date = dv.date AND l.clientId = dv.clientId AND CAST(l.visitId AS STRING) = dv.visitId
INNER JOIN job_ad_info j ON l.ad_id = j.ad_id
WHERE l.date BETWEEN dr.window_start AND dr.window_end AND l.is_bot IS NULL AND l.ad_id IS NOT NULL
"""

_PTY_FOCUS_CATEGORIES = {'Apartments', 'Houses', 'Rooms'}
_FOCUS_REGIONS = {'HCM', 'BD'}
_JOB_FOCUS_TYPES = {
    'Bán hàng', 'Nhân viên kinh doanh', 'Nhân viên phục vụ', 'Tài xế giao hàng xe máy',
    'Công nhân', 'Nhân viên kho vận', 'Bảo vệ', 'Tài xế ô tô',
}


def _seg_int(v):
    return int(v) if v is not None else 0


def _seg_build_view(bucket_rows, dwl_raw_rows, key_fields):
    """Build one rollup view at exactly `key_fields` granularity. adview/lead are plain counts —
    safe to re-sum across bucket_rows at ANY grouping. dwl is NOT safe to re-sum across groupings,
    so it's computed fresh here as COUNT(DISTINCT date||clientId) directly from the raw rows, at
    this exact grain — never by summing a finer grain's distinct count (that's the 2026-08-18 bug).
    key_fields=[] gives a single grand-total row."""
    totals = {}
    for r in bucket_rows:
        k = tuple(r[f] for f in key_fields)
        d = totals.setdefault(k, {'adview': 0, 'lead': 0})
        d['adview'] += _seg_int(r['adview_total'])
        d['lead']   += _seg_int(r['lead_total'])
    dwl_sets = {}
    for r in dwl_raw_rows:
        k = tuple(r[f] for f in key_fields)
        dwl_sets.setdefault(k, set()).add((str(r['date']), r['clientId']))
    out = []
    for k in set(totals) | set(dwl_sets):
        row = dict(zip(key_fields, k))
        row['adview'] = totals.get(k, {}).get('adview', 0)
        row['lead']   = totals.get(k, {}).get('lead', 0)
        row['dwl']    = len(dwl_sets.get(k, ()))
        out.append(row)
    return sorted(out, key=lambda r: -r['dwl'])


def build_segment_mix():
    """Digital-campaign lead mix by our own universal attributes (not the vertical's live
    quadrant — see D-020 in chotot-digital's context/decisions.md). Returns None (leaving
    existing embedded data untouched) on query failure, same pattern as build_url_tracking()."""
    try:
        pty_bucket = q(_PTY_BUCKET_SQL)
        pty_dwl_raw = q(_PTY_DWL_RAW_SQL)
        job_bucket = q(_JOB_BUCKET_SQL)
        job_dwl_raw = q(_JOB_DWL_RAW_SQL)
    except Exception as e:
        print(f"  ❌ Segment Mix query FAILED: {type(e).__name__}: {e}")
        return None

    for r in pty_bucket + pty_dwl_raw:
        r['is_focus_category'] = 'focus' if r['category_name'] in _PTY_FOCUS_CATEGORIES else 'non-focus'
        r['is_focus_region']   = 'focus' if r['region'] in _FOCUS_REGIONS else 'non-focus'
    for r in job_bucket + job_dwl_raw:
        r['is_focus_job_type'] = 'focus' if r['job_type'] in _JOB_FOCUS_TYPES else 'non-focus'
        r['is_focus_region']   = 'focus' if r['region'] in _FOCUS_REGIONS else 'non-focus'

    pty_totals_rows = _seg_build_view(pty_bucket, pty_dwl_raw, [])
    pty_totals = pty_totals_rows[0] if pty_totals_rows else {'adview': 0, 'lead': 0, 'dwl': 0}
    pty_tiers      = _seg_build_view(pty_bucket, pty_dwl_raw, ['tier'])
    pty_categories = _seg_build_view(pty_bucket, pty_dwl_raw, ['category_name', 'is_focus_category'])
    pty_regions    = _seg_build_view(pty_bucket, pty_dwl_raw, ['region', 'is_focus_region'])
    pty_detail     = _seg_build_view(
        pty_bucket, pty_dwl_raw,
        ['ad_type', 'category_name', 'is_focus_category', 'region', 'is_focus_region', 'tier'])

    job_totals_rows = _seg_build_view(job_bucket, job_dwl_raw, [])
    job_totals = job_totals_rows[0] if job_totals_rows else {'adview': 0, 'lead': 0, 'dwl': 0}
    job_focus   = _seg_build_view(job_bucket, job_dwl_raw, ['is_focus_job_type'])
    job_regions = _seg_build_view(job_bucket, job_dwl_raw, ['region', 'is_focus_region'])
    job_detail  = _seg_build_view(
        job_bucket, job_dwl_raw, ['job_type', 'is_focus_job_type', 'region', 'is_focus_region'])

    def pct(part, total):
        return round(part / total * 100, 1) if total else 0.0

    print(f"  PTY Segment Mix: {len(pty_detail)} groups, {pty_totals['dwl']} total DWL — tier split "
          + ", ".join(f"{t['tier']}={pct(t['dwl'], pty_totals['dwl'])}%" for t in pty_tiers))
    print(f"  JOB Segment Mix: {len(job_detail)} groups, {job_totals['dwl']} total DWL — focus split "
          + ", ".join(f"{f['is_focus_job_type']}={pct(f['dwl'], job_totals['dwl'])}%" for f in job_focus))

    return {
        'dataAsOf': (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d'),
        'windowDays': 30,
        'PTY': {'totals': pty_totals, 'tiers': pty_tiers, 'categories': pty_categories,
                 'regions': pty_regions, 'detail': pty_detail},
        'JOB': {'totals': job_totals, 'focus': job_focus, 'regions': job_regions,
                 'detail': job_detail},
    }


print("Querying Segment Mix (PTY category x tier, JOB job_type)...")
segment_mix = build_segment_mix()

# ── 3f. Active Tests — District 12 (PTY Sell) ───────────────────────────────
# New top-level "Active Tests" tab (holds any currently-running test/initiative going forward;
# for now exactly one section). Two parts, stacked:
#   Part 1 — broad channel trend: ALL traffic (no campaign-name filter) landing on a District 12
#     PTY Sell listing, daily DAU/DWL/Lead by channel (Google/Meta/Zalo), 2026-07-24 through the
#     latest complete day.
#   Part 2 — named-campaign table: DAU/DWL/Lead/Cost/CP-DWL/cost-per-lead for the specific
#     campaigns built for D12 (Google PMax, Meta old+new). By design Part 1's total must be
#     LARGER than Part 2's combined (broad/incidental traffic vs. deliberately-targeted campaigns)
#     — if it's ever the other way round, something is mis-scoped; check before trusting the tab.
#
# D12 ad filter (established 2026-08-18, see chotot-digital's context/shared/measurement/
# warehouse-model.md hard checks + tools/queries/pty/sell-houses-q12-match-rate-reconciliation.sql):
# city_name='Tp Hồ Chí Minh' AND district_id=13107 AND ad_type='sell' AND vertical='PTY' AND
# category IN (1010,1020) (Houses+Apartments, both confirmed 100% PTY-exclusive category codes).
# district_id/district_name alone are NOT city- or vertical-specific — always pair with city_name
# AND vertical/category, or the query silently pulls in ~11 other provinces' worth of "district 12"
# and every other vertical's "sell" listings (caused a real ~12x DWL inflation once already).
#
# Channel detection — verified fresh against real BigQuery data 2026-08-19 (not assumed from
# memory), independently cross-checked a second time mid-build:
#   Google: channelGrouping IN ('Paid Search','Display') AND source IN ('google','google_search')
#   Meta:   source = 'facebook' (medium='display', channelGrouping='Display' — the SAME
#           channelGrouping bucket as Google Display/PMax; `source` MUST be checked too or Meta
#           traffic silently gets counted as Google). Confirmed on the real campaigns:
#           fb_growth_pty_web_pro_thodia_bau_072126_atc_HCM and _sell_..._180826_HCM both show
#           source='facebook'/medium='display'/channelGrouping='Display'.
#   Zalo:   NO reliable campaign-specific signal found. Every `source='zalo'` row in
#           traffic_visit_detail (campaign='zalo', medium='zalo') is generic, pre-existing organic
#           Zalo referral/share-button traffic — confirmed present at ~11-24 DAU/day on D12 PTY
#           Sell listings every day back through 2026-07-24, well before the new paid campaign's
#           Aug18 launch. The new Zalo campaign (₫300k budget, Aug18-31, manually-tagged UTM) has
#           no distinguishable utm_campaign value in the warehouse as of the data checked — using
#           source='zalo' would misattribute years of organic baseline to the new initiative.
#           Ships as an honest zero / "not yet detected" (not fabricated) until a real UTM shows up.
_D12_ADS_CTE = """
  SELECT ad_id
  FROM `chotot-dwh.chotot_data.ad`
  WHERE city_name = 'Tp Hồ Chí Minh' AND district_id = 13107 AND ad_type = 'sell'
    AND vertical = 'PTY' AND category IN (1010,1020)
"""

_D12_START_DATE = '2026-07-24'

_D12_TEST_PART1_SQL = f"""
DECLARE start_date DATE DEFAULT '{_D12_START_DATE}';
DECLARE end_date DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY);

WITH d12_ads AS ({_D12_ADS_CTE}),
visits AS (
  SELECT date, clientId, CAST(visitId AS STRING) AS visitId, channelGrouping, source
  FROM `chotot-dwh.chotot_data.traffic_visit_detail`
  WHERE date BETWEEN start_date AND end_date AND (is_bot IS NULL OR is_bot = FALSE)
),
channel_map AS (
  SELECT date, clientId, visitId,
    CASE
      WHEN channelGrouping IN ('Paid Search','Display') AND source IN ('google','google_search') THEN 'Google'
      WHEN source = 'facebook' THEN 'Meta'
      ELSE 'Other'
    END AS channel
  FROM visits
),
adviews AS (
  SELECT p.date, p.clientId, CAST(p.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_pageview_detail` p
  INNER JOIN d12_ads a ON p.ad_id = a.ad_id
  WHERE p.date BETWEEN start_date AND end_date AND p.page_type IN ('adview','ad_view')
),
leads AS (
  SELECT l.date, l.clientId, CAST(l.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_lead_detail` l
  INNER JOIN d12_ads a ON l.ad_id = a.ad_id
  WHERE l.date BETWEEN start_date AND end_date AND l.is_bot IS NULL
),
dau_rows AS (
  SELECT DISTINCT av.date, av.clientId, cm.channel
  FROM adviews av
  INNER JOIN channel_map cm ON av.date=cm.date AND av.clientId=cm.clientId AND av.visitId=cm.visitId
  WHERE cm.channel IN ('Google','Meta')
),
dwl_rows AS (
  SELECT DISTINCT l.date, l.clientId, cm.channel
  FROM leads l
  INNER JOIN channel_map cm ON l.date=cm.date AND l.clientId=cm.clientId AND l.visitId=cm.visitId
  WHERE cm.channel IN ('Google','Meta')
),
lead_rows AS (
  SELECT l.date, cm.channel, l.clientId
  FROM leads l
  INNER JOIN channel_map cm ON l.date=cm.date AND l.clientId=cm.clientId AND l.visitId=cm.visitId
  WHERE cm.channel IN ('Google','Meta')
)
SELECT
  COALESCE(d.date, w.date, le.date) AS date,
  COALESCE(d.channel, w.channel, le.channel) AS channel,
  COALESCE(d.dau,0) AS dau, COALESCE(w.dwl,0) AS dwl, COALESCE(le.lead_cnt,0) AS lead_cnt
FROM (SELECT date, channel, COUNT(*) AS dau FROM dau_rows GROUP BY 1,2) d
FULL OUTER JOIN (SELECT date, channel, COUNT(*) AS dwl FROM dwl_rows GROUP BY 1,2) w
  ON d.date=w.date AND d.channel=w.channel
FULL OUTER JOIN (SELECT date, channel, COUNT(*) AS lead_cnt FROM lead_rows GROUP BY 1,2) le
  ON COALESCE(d.date,w.date)=le.date AND COALESCE(d.channel,w.channel)=le.channel
ORDER BY 1,2
"""

D12_CAMPAIGNS = [
    {'name': 'digital_dau_pty_sell_houses_q12_pmax_lead_2026',
     'label': 'Google PMax — sell_houses_q12_pmax_lead_2026', 'channel': 'Google'},
    {'name': 'fb_growth_pty_web_pro_thodia_bau_072126_atc_HCM',
     'label': 'Meta (old) — fb_growth_pty_web_pro_thodia_bau_072126_atc_HCM', 'channel': 'Meta'},
    {'name': 'fb_growth_pty_web_pro_sell_thodia_bau_180826_HCM',
     'label': 'Meta (new) — fb_growth_pty_web_pro_sell_thodia_bau_180826_HCM', 'channel': 'Meta'},
]
_D12_CAMP_NAMES_SQL = ', '.join(f"'{c['name']}'" for c in D12_CAMPAIGNS)

_D12_TEST_PART2_SQL = f"""
DECLARE start_date DATE DEFAULT '{_D12_START_DATE}';
DECLARE end_date DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY);
DECLARE camps ARRAY<STRING> DEFAULT [{_D12_CAMP_NAMES_SQL}];

WITH d12_ads AS ({_D12_ADS_CTE}),
visits AS (
  SELECT date, clientId, CAST(visitId AS STRING) AS visitId, campaign
  FROM `chotot-dwh.chotot_data.traffic_visit_detail`
  WHERE date BETWEEN start_date AND end_date AND (is_bot IS NULL OR is_bot = FALSE)
    AND campaign IN UNNEST(camps)
),
adviews AS (
  SELECT p.date, p.clientId, CAST(p.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_pageview_detail` p
  INNER JOIN d12_ads a ON p.ad_id = a.ad_id
  WHERE p.date BETWEEN start_date AND end_date AND p.page_type IN ('adview','ad_view')
),
leads AS (
  SELECT l.date, l.clientId, CAST(l.visitId AS STRING) AS visitId
  FROM `chotot-dwh.chotot_data.traffic_lead_detail` l
  INNER JOIN d12_ads a ON l.ad_id = a.ad_id
  WHERE l.date BETWEEN start_date AND end_date AND l.is_bot IS NULL
),
dau_rows AS (
  SELECT DISTINCT av.date, av.clientId, v.campaign
  FROM adviews av
  INNER JOIN visits v ON av.date=v.date AND av.clientId=v.clientId AND av.visitId=v.visitId
),
dwl_rows AS (
  SELECT DISTINCT l.date, l.clientId, v.campaign
  FROM leads l
  INNER JOIN visits v ON l.date=v.date AND l.clientId=v.clientId AND l.visitId=v.visitId
),
lead_rows AS (
  SELECT l.date, v.campaign, l.clientId
  FROM leads l
  INNER JOIN visits v ON l.date=v.date AND l.clientId=v.clientId AND l.visitId=v.visitId
)
SELECT
  c AS campaign,
  (SELECT COUNT(*) FROM dau_rows WHERE campaign=c) AS total_dau,
  (SELECT COUNT(*) FROM dwl_rows WHERE campaign=c) AS total_dwl,
  (SELECT COUNT(*) FROM lead_rows WHERE campaign=c) AS total_lead
FROM UNNEST(camps) AS c
"""

_D12_COST_SQL = f"""
SELECT campaign, ROUND(SUM(spend_vnd)) AS cost
FROM `chotot-dwh.ct_digital.kiet_digital_campaign_daily`
WHERE date BETWEEN '{_D12_START_DATE}' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND campaign IN ({_D12_CAMP_NAMES_SQL})
GROUP BY 1
"""


def _d12_datestr(v):
    return v.strftime('%Y-%m-%d') if hasattr(v, 'strftime') else str(v)


def build_d12_test():
    """Active Tests tab — District 12 (PTY Sell). Returns None (leaving existing embedded data
    untouched) on query failure, same pattern as build_url_tracking()/build_segment_mix()."""
    try:
        part1_rows = q(_D12_TEST_PART1_SQL)
        part2_rows = q(_D12_TEST_PART2_SQL)
        cost_rows = q(_D12_COST_SQL)
    except Exception as e:
        print(f"  ❌ D12 Active Test query FAILED: {type(e).__name__}: {e}")
        return None

    # Part 1 — daily series by channel. Zalo is always zero here (see header note) — no reliable
    # campaign-specific signal exists yet, and reusing source='zalo' would fold in pre-existing
    # organic Zalo referral traffic that has nothing to do with the new campaign.
    by_date_channel = {}
    dates = set()
    for r in part1_rows:
        d = _d12_datestr(r['date'])
        dates.add(d)
        by_date_channel[(d, r['channel'])] = {
            'dau': int(r['dau'] or 0), 'dwl': int(r['dwl'] or 0), 'lead': int(r['lead_cnt'] or 0)}

    daily = []
    totals = {'Google': {'dau': 0, 'dwl': 0, 'lead': 0},
              'Meta': {'dau': 0, 'dwl': 0, 'lead': 0},
              'Zalo': {'dau': 0, 'dwl': 0, 'lead': 0}}
    for d in sorted(dates):
        row = {'date': d}
        for ch in ('Google', 'Meta', 'Zalo'):
            m = by_date_channel.get((d, ch), {'dau': 0, 'dwl': 0, 'lead': 0})
            key = ch.lower()
            row[f'{key}Dau'] = m['dau']; row[f'{key}Dwl'] = m['dwl']; row[f'{key}Lead'] = m['lead']
            totals[ch]['dau'] += m['dau']; totals[ch]['dwl'] += m['dwl']; totals[ch]['lead'] += m['lead']
        daily.append(row)

    # Part 2 — named campaigns. DAU/DWL/Lead computed the same way as Part 1 (direct distinct-count
    # at the campaign grain, from raw joined rows — never summed from a finer grain, per the DWL
    # hard check in warehouse-model.md). Cost comes from kiet_digital_campaign_daily (the campaign's
    # own total spend, not scoped to D12 — CP-DWL/cost-per-lead show how efficiently that spend
    # produces D12-specific results).
    cost_by_camp = {r['campaign']: float(r['cost']) for r in cost_rows if r['cost'] is not None}
    metrics_by_camp = {r['campaign']: {'dau': int(r['total_dau'] or 0), 'dwl': int(r['total_dwl'] or 0),
                                        'lead': int(r['total_lead'] or 0)} for r in part2_rows}

    campaigns = []
    for c in D12_CAMPAIGNS:
        m = metrics_by_camp.get(c['name'], {'dau': 0, 'dwl': 0, 'lead': 0})
        cost = cost_by_camp.get(c['name'])
        cp_dwl = round(cost / m['dwl']) if (cost is not None and m['dwl']) else None
        cp_lead = round(cost / m['lead']) if (cost is not None and m['lead']) else None
        campaigns.append({
            'name': c['name'], 'label': c['label'], 'channel': c['channel'],
            'dau': m['dau'], 'dwl': m['dwl'], 'lead': m['lead'],
            'cost': round(cost) if cost is not None else None,
            'cpDwl': cp_dwl, 'cpLead': cp_lead, 'placeholder': False,
        })
    # Zalo placeholder — no identifiable campaign-specific utm_campaign value in the warehouse yet
    # (see header note). Per this task's own instruction: don't guess, flag it instead.
    campaigns.append({
        'name': None, 'label': 'Zalo — new D12 campaign (₫300k, Aug18-31)', 'channel': 'Zalo',
        'dau': None, 'dwl': None, 'lead': None, 'cost': None, 'cpDwl': None, 'cpLead': None,
        'placeholder': True,
        'note': "not yet identifiable in warehouse — confirm exact utm_campaign value; "
                "source='zalo' alone matches pre-existing organic Zalo referral traffic, not this campaign",
    })

    part1_totals = totals
    part1_sum = {k: sum(v[k] for v in totals.values()) for k in ('dau', 'dwl', 'lead')}
    part2_sum = {k: sum((c[k] or 0) for c in campaigns) for k in ('dau', 'dwl', 'lead')}

    print(f"  D12 Active Test — Part1 (all traffic) totals: "
          f"dau={part1_sum['dau']} dwl={part1_sum['dwl']} lead={part1_sum['lead']}")
    print(f"  D12 Active Test — Part2 (named campaigns) totals: "
          f"dau={part2_sum['dau']} dwl={part2_sum['dwl']} lead={part2_sum['lead']}")
    if part1_sum['dwl'] <= part2_sum['dwl']:
        print("  ⚠️  Part1 DWL is NOT larger than Part2 DWL — investigate scoping before trusting this tab")
    else:
        print("  ✅ Part1 > Part2 on DWL, as expected (broad/incidental traffic vs. deliberately-targeted campaigns)")

    return {
        'dataAsOf': (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d'),
        'startDate': _D12_START_DATE,
        'part1': {'daily': daily, 'totals': part1_totals},
        'part2': {'campaigns': campaigns},
        'notes': {
            'google': "channelGrouping IN ('Paid Search','Display') AND source IN ('google','google_search')",
            'meta': "source='facebook' (medium='display', channelGrouping='Display' — same bucket as "
                    "Google Display/PMax, so source must be checked too, not channelGrouping alone)",
            'zalo': "no reliable campaign-specific signal found — source='zalo' in the warehouse is "
                    "generic pre-existing organic Zalo referral/share traffic (campaign='zalo', "
                    "medium='zalo'), already present daily before the new campaign's Aug18 launch, "
                    "so it is NOT used here; shown as zero/not-detected rather than misattributed",
        },
    }


print("Querying D12 Active Test (District 12 PTY Sell)...")
d12_test = build_d12_test()

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

def js_url_tracking(data):
    """Serialize the URL_TRACKING dict as a JS const. A plain dict of ints/strings/lists is
    valid JSON, and JSON is a strict subset of JS object-literal syntax, so json.dumps is a
    sufficient (and simplest) JS serializer here."""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return ('// URL Tracking data (PTY/JOB Google Ads landing-page focus tracking) — auto-updated by\n'
            '// scripts/update_marketplace.py from the "PTY-URL"/"JOB-URL" tabs of the "Digital demand\n'
            '// campaigns - LDP" Google Sheet, classified via scripts/url_tracking_classifier.py.\n'
            f'const URL_TRACKING = {body};')

def js_segment_mix(data):
    """Serialize the SEGMENT_MIX dict as a JS const — same json.dumps-as-JS-literal approach as
    js_url_tracking (a plain dict of ints/strings/lists is valid JSON, and JSON is a strict
    subset of JS object-literal syntax)."""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return ('// Lead Segment Mix data (PTY: ad_type x category x region x price tier; JOB:\n'
            '// job_type x region) — auto-updated by scripts/update_marketplace.py, direct\n'
            '// BigQuery, rolling 30-day window. See chotot-digital repo context/decisions.md D-020.\n'
            f'const SEGMENT_MIX = {body};')

def js_d12_test(data):
    """Serialize the D12_TEST dict as a JS const — same json.dumps-as-JS-literal approach as
    js_segment_mix/js_url_tracking."""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return ('// Active Tests — District 12 (PTY Sell) data — auto-updated by\n'
            '// scripts/update_marketplace.py, direct BigQuery, from 2026-07-24 through the latest\n'
            '// complete day. See build_d12_test() in that script for the full method + channel-\n'
            '// detection notes (Google/Meta/Zalo source verification).\n'
            f'const D12_TEST = {body};')

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

if url_tracking is not None:
    _urlt_js = js_url_tracking(url_tracking)
    html = re.sub(r'// URL Tracking data[\s\S]*?const URL_TRACKING = \{[\s\S]*?^\};',
                  lambda _: _urlt_js, html, flags=re.MULTILINE)
    print("  URL_TRACKING updated")
else:
    print("  ⚠️  URL_TRACKING left untouched (Sheets read failed — see error above)")

if segment_mix is not None:
    _segmix_js = js_segment_mix(segment_mix)
    html = re.sub(r'// Lead Segment Mix data[\s\S]*?const SEGMENT_MIX = \{[\s\S]*?^\};',
                  lambda _: _segmix_js, html, flags=re.MULTILINE)
    print("  SEGMENT_MIX updated")
else:
    print("  ⚠️  SEGMENT_MIX left untouched (query failed — see error above)")

if d12_test is not None:
    _d12_js = js_d12_test(d12_test)
    html = re.sub(r'// Active Tests[\s\S]*?const D12_TEST = \{[\s\S]*?^\};',
                  lambda _: _d12_js, html, flags=re.MULTILINE)
    print("  D12_TEST updated")
else:
    print("  ⚠️  D12_TEST left untouched (query failed — see error above)")

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
