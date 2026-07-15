# Chotot Marketing Dashboard — Onboarding cho Kiệt

> Người giao: Chile  
> Repo: `chile-ct/ct-demand-marketing`  
> Live URL: https://chile-ct.github.io/ct-demand-marketing/  
> Last updated: Jul 2026

---

## 1. Tổng quan kiến trúc

Dashboard là **một file HTML duy nhất** — không có backend, không có server, không có npm run dev phức tạp.

```
src/index.html        ← file nguồn duy nhất, toàn bộ code ở đây (JSX + data ~15.700 dòng)
build.js              ← Babel compile JSX → plain JS, output ra dist/ và index.html gốc
dist/index.html       ← file compiled, GitHub Pages serve file này
index.html            ← copy của dist/, dùng cho GitHub Pages root
sw.js                 ← Service Worker: đảm bảo browser luôn fetch HTML mới nhất (no-store)
scripts/update_marketplace.py  ← auto-update data từ BigQuery, chạy daily qua GitHub Actions
```

**Build workflow:**
```bash
node build.js          # compile src/index.html → dist/index.html + index.html
git add -A && git commit && git push   # GitHub Pages tự deploy
```

**Dev workflow:**
```bash
cd dist && python3 -m http.server 3458   # serve locally tại localhost:3458
```

---

## 2. Data sources

| Nguồn | Variable | Dùng cho |
|-------|----------|----------|
| `chotot_mtm.dashboard__dau_vertical_monthly` | `VERT_ACT` | Authoritative DAU/DwL/Lead per vertical |
| `chotot_mtm.dashboard__dau_vertical_monthly` | `ACT` | Platform total DAU/DwL/Lead/MAU/MwL |
| `ct_product_analytics.daumaulead_mkt_rp` | `DATA` (raw array) | DAU/DwL/Lead per channel × vertical × campaign |
| BigQuery `chotot_mtm` MAU tables | `ACT_MAU`, `vertMonthMau()` | MAU/MwL/DAU/MAU ratio |
| Google Sheets (FC & Actual cost) | `costData` | DwL campaign spend |
| KPI hardcode | `KPI_FC0`, `KPI_FC1`, `KPI_VERT_FC0/FC1` | KPI targets |

### Quy tắc authoritative (quan trọng nhất):
- **Platform total** → luôn dùng `ACT[m]` (từ `chotot_mtm`)
- **Per-vertical total** → luôn dùng `VERT_ACT[v][m]` (từ `chotot_mtm`)
- **Per-channel data** → dùng `DATA` (từ `daumaulead_mkt_rp`) — raw, có thể overcount
- Mỗi breakdown table (by channel, by vertical, by user type) đều có dòng **Unattributed** = ACT total − Σ breakdown, đảm bảo cộng lại luôn = ACT
- Số âm ở Unattributed = overcount (multi-touch attribution: 1 user bị đếm ở nhiều channel trong tháng)

---

## 3. Các constants quan trọng

```js
// src/index.html ~line 184
MONTHS   = ["2026-01", ..., "2026-07"]   // auto-update hàng tháng
ML       = {"2026-01":"T1/26", ...}       // display labels
VERTS    = ["PTY","JOB","VEH","GDS"]
CHANNELS = ["Direct","Organic Search","Growth (Paid)","Growth (CRM)","(Other)"]
VC       = {PTY:"#6366f1", JOB:"#f59e0b", VEH:"#10b981", GDS:"#ef4444"}  // màu vertical
METRICS  = [{key:"dau",...}, {key:"dwl",...}, {key:"lead",...}, {key:"mau",...},
            {key:"mauLead",...}, {key:"dauMau",...}]

// Data objects
ACT[m]         → {dau, dwl, lead, mau, mauLead, dauMau}   // platform total
VERT_ACT[v][m] → {dau, dwl, lead, mau, mauLead}           // per-vertical
DATA           → [{m, v, ch, campaign, dau, dwl, lead, nd, rd}, ...]  // raw channel data
```

---

## 4. Các helper functions chính

```js
// ~line 571 — aggregate DATA by vertical+month
vertMonthSum(v, m)          → {dau, dwl, lead}

// ~line 579 — aggregate DATA by channel+month (all verticals)
chMonthSum(ch, m)           → {dau, dwl, lead}

// ~line 928 — MAU/MwL per vertical (từ VERT_ACT scaled)
vertMonthMau(v, m)          → {mau, mauLead, dauMau}

// ~line 941 — MAU/MwL per channel (phân bổ theo tỷ lệ DAU)
chMonthMau(ch, m)           → {mau, mauLead}

// ~line 619 — New/Returning users per channel+month
chMonthMetricUT(ch, m, key) → {nv, rv}

// ~line 644 — New/Returning users per vertical+month
vertMonthMetricUT(v, m, key)→ {nv, rv}
```

---

## 5. Cấu trúc sections (Navigation)

| Section | Component | ID | Nội dung |
|---------|-----------|-----|----------|
| Executive Summary | `ExecSummary()` | `#exec` | KPI vs Actual, Actual of total platform, Actual by User Type |
| Trendline Vertical | `VerticalSection()` | `#vertical` | Line charts per vertical, Actual by Vertical (All/PTY/JOB/VEH/GDS), NV/RV by vertical |
| Trendline Channel | `ChannelSection()` | `#channel` | Line chart by channel, Chi tiết by channel (All/PTY/JOB/VEH/GDS chips), NV/RV by channel |
| Detail Table | `DetailSection()` | `#detail` | By Vertical × Channel matrix |
| Growth Performance | `GrowthSection()` | `#growth` | Growth Paid & CRM charts, DAU by Channel (All/PTY/JOB/VEH/GDS), Cost & unit economy |
| Campaign Breakdown | `CampaignSection()` | `#campaign` | Link → Campaign Audit Dashboard, campaign-level data |
| Cost Input | `CostInputSection()` | n/a | Manual input DwL cost |
| Marketplace Health | `MarketplaceHealth()` | `#mh-*` | Supply/demand health từ BQ tracking table |

---

## 6. Auto-update data (GitHub Actions)

File `.github/workflows/update_data.yml` chạy daily lúc 11:00 AM GMT+7:

1. Pull `dashboard__dau_vertical_monthly` từ BigQuery (`chotot_mtm`)
2. Pull `daumaulead_mkt_rp` from `ct_product_analytics` 
3. Update các constants trong `src/index.html` (regex replace)
4. Chạy `node build.js`
5. Commit & push nếu có thay đổi

**Secret cần có trong repo:** `GCP_USER_CREDENTIALS` (service account JSON)

---

## 7. Cách thêm/sửa data mới

### Thêm tháng mới:
Auto rồi — `update_marketplace.py` tự detect `latest_m` từ BQ và update `MONTHS` array.

### Sửa số ACT (khi có report chính thức):
```js
// src/index.html ~line 471
const ACT = {
  "2026-01":{dau:443370, dwl:45651, lead:4386843, mau:6155280, mauLead:621427, dauMau:7.2},
  // ...thêm/sửa tháng ở đây
};
```

### Sửa số VERT_ACT per vertical:
```js
// src/index.html ~line 383
const VERT_ACT = {
  PTY: {"2026-01":{dau:..., dwl:..., lead:..., mau:..., mauLead:...}, ...},
  JOB: {...},
  VEH: {...},
  GDS: {...},
};
```

### Sửa KPI:
```js
// ~line 206 — KPI FC0 (primary)
const KPI_FC0 = {"2026-01":{dau:..., dwl:..., lead:...}, ...};
// ~line 262 — KPI per vertical
const KPI_VERT_FC0 = {PTY:{...}, JOB:{...}, VEH:{...}, GDS:{...}};
```

---

## 8. Gotchas quan trọng

1. **MAU cho channel** — không có data MAU trực tiếp per channel. Code dùng proxy: tính share DAU của channel trong vertical, rồi nhân với vertical MAU. Nên số MAU by channel chỉ là ước tính.

2. **T5-T7 DATA** — rows trong `DATA` array cho T5-T7 là synthetic (scaled từ VERT_ACT proportions), không phải raw từ BQ. Xem comments trong `update_marketplace.py`.

3. **Negative Unattributed** ở Detail Table = BÌNH THƯỜNG. `daumaulead_mkt_rp` đếm user ở mỗi channel họ dùng trong tháng → Σ channel > unique users. Không phải data bug.

4. **Platform total > Σ vertical** — ~60–96K users/tháng browse platform nhưng không vào vertical cụ thể nào. Xuất hiện trong `ACT` nhưng không trong `VERT_ACT`. Đây là lý do "Unattributed" trong breakdown by vertical luôn dương.

5. **Service Worker (`sw.js`)** — đảm bảo browser luôn fetch HTML fresh khi có deploy mới. Nếu bạn thấy version cũ sau push, thử đóng hẳn tab và mở lại.

6. **Chỉ edit `src/index.html`, KHÔNG edit `dist/` hay root `index.html`** — chúng được generate bởi `build.js` và sẽ bị overwrite.

---

## 9. Pending / Known issues

- **Bảng "Actual by Platform"** (desktop/msite/android/ios) chưa implement — data không có trong data source hiện tại, cần pull thêm BQ table có `platform_type` breakdown.
- T7/2026 data chưa đủ tháng (partial month).

---

## 10. Quick start

```bash
git clone https://github.com/chile-ct/ct-demand-marketing.git
cd ct-demand-marketing
npm install                               # chỉ cần @babel/core, @babel/preset-react
node build.js                             # compile src → dist
cd dist && python3 -m http.server 3458   # xem tại localhost:3458
```

Sau khi edit `src/index.html`:
```bash
node build.js && git add -A && git commit -m "feat: ..." && git push
# GitHub Pages auto deploy trong ~1-2 phút
```
