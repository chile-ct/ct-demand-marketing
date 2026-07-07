# Channel Grouping — Proposed Reclassification Rules

> Nguồn: phân tích `chotot_data.traffic_visit_detail`, T1–T6/2026  
> Ngày: 2026-07-03  

---

## Vấn đề hiện tại

Channel `(Other)` trong `daumaulead_mkt_rp` đang chứa ~3.8M DAU/tháng bị bucket lẫn lộn gồm:
- Traffic thực có UTM tagged nhưng chưa được map đúng channel
- Internal navigation (floating banner, web_to_app) — **không phải traffic đến từ ngoài**

Bảng `daumaulead_mkt_rp` không có UTM columns nên không thể fix trực tiếp — cần update ở pipeline upstream.

---

## Proposed Rules (áp dụng tại bước channel grouping)

### Rule 1 — Reclassify → Growth (Paid)
```
source = 'facebook' AND medium = 'digital_ad'
```
- Ví dụ campaign: `digital_pty_promax_may_2026`
- T1–T6/2026: ~19,400 users

### Rule 2 — Reclassify → Growth (CRM)
```
source = 'noti' AND medium IN ('dau', 'new')
```
- Ví dụ campaign: `convertpty`, `biz_gds_subs_elt`, `5010`
- T1–T6/2026: ~21,000 users

### Rule 3 — Exclude khỏi DAU (internal navigation)
Traffic này là internal app flow, không đại diện cho traffic đến từ channel ngoài:
```
source IN ('web_to_app', 'inapp', 'popup', 'in_app')
OR medium LIKE '%floating%'
OR medium LIKE '%top_banner%'
```
| Source/Medium | Ví dụ | Users T1–T6 |
|---|---|---|
| `(direct)` / `floating` | pty_livestream, job_ct_live floating banners | ~317K |
| `in_app` / `floating`, `inapp_mess` | lo_to_viec_lam | ~87K |
| `inapp` / `floating` | digital_veh_mc campaigns | ~37K |
| `web_to_app` / `top_banner_*` | web_to_app_gds/pty/veh/job | ~24K |
| `popup` / `new`, `(none)` | biz_gds_shop_elt | ~18K |
| **Tổng exclude** | | **~750K users** |

---

## Composition của (Other) T1–T6/2026

| Nhóm | Users | % |
|---|---|---|
| Null / untagged (no UTM) | ~2.0M | 63% |
| Zalo messenger (link share) | 324K | 10% |
| Share buttons (ad_view / share_buttons) | ~271K | 9% |
| Internal navigation → **nên exclude** | ~750K | 24% |
| Noti push → **nên về Growth CRM** | ~21K | <1% |
| Facebook digital_ad → **nên về Growth Paid** | ~19K | <1% |
| ChatGPT / AI referral | ~47K | 1.5% |
| onflow push campaigns | ~117K | 4% |
| Khác | còn lại | — |

---

## Lưu ý thêm

- **`onflow` / `push`** (~117K): push notification của Growth CRM (onflow tool). Cần confirm với team CRM xem có nên map vào Growth (CRM) không — hiện chưa rõ ràng đủ để add rule.
- **`zalo` / `zalo`** (324K): traffic organic từ link share Zalo — nên để lại trong (Other) hoặc tạo channel riêng "Social/Zalo".
- **`chatgpt.com`** (~47K): AI referral traffic — nên để Referral hoặc tạo nhóm riêng nếu trend tiếp tục tăng.
- **63% null UTM**: phần lớn là in-app navigation hoặc sessions thiếu tracking tag — cần review MTM tracking setup.

---

## Action Items

| # | Action | Owner | Priority |
|---|---|---|---|
| 1 | Update channel grouping logic trong `daumaulead_mkt_rp` pipeline theo Rule 1 & 2 | Data Eng | Medium |
| 2 | Exclude internal navigation (Rule 3) khỏi DAU metric trong pipeline | Data Eng | High |
| 3 | Review `onflow/push` → có nên map về Growth (CRM)? | Growth team | Low |
| 4 | Review MTM tracking setup để giảm 63% null UTM | Data/Marketing | Medium |
