# Taobao Open Platform — Official API Contract Note (Product Search only)

Scope per instruction: **only Taobao product search/recommendation**. No
cart, order, payment, redirect, or EffectApproval handoff is in scope.

Source of truth (latest): `docs/taobao/api文档` in the main Aidison
workspace (`/home/aluo/project/aidison_ws/docs/taobao/api文档`) —
`taobao.tbk.dg.material.optional.upgrade`（淘宝客-推广者-物料搜索升级版，通用物料搜索API（导购）），免费不需用户授权.
Read 2026-08-10. This supersedes the earlier login-gated findings that
marked the whole contract `[X]`/not_checked.

## Verified contract (from the official doc)

- **Endpoint (HTTPS):** `https://eco.taobao.com/router/rest`.
- **Common params:** `method`, `app_key`, `timestamp`
  (`yyyy-MM-dd HH:mm:ss`, **GMT+8**), `v=2.0`, `sign_method`
  (`hmac`/`md5`/`hmac-sha256`), `sign`, optional `format=json`.
- **Search params kept by this adapter:**
  - `q` — keyword query. Affiliate links are **not** accepted as `q`; a
    title search returning a single result requires consumer
    price-comparison scene-ID2 permission to expose 推广链接/商品id.
  - `material_id` — default `80309` when omitted (official default);
    consumer-side personalised material `17004` falls back to `80309`.
  - `adzone_id` — **required**; the last numeric segment of
    `mm_xxx_xxx_12345678`.
  - `page_no` (default 1), `page_size` (default 20, range 1~100).
- **Response envelope:** `tbk_dg_material_optional_upgrade_response`
  containing `total_results` and `result_list.map_data[]`. Items used:
  `item_id`, `item_basic_info.title/shop_title`,
  `price_promotion_info` (`final_promotion_price` 预估到手价,
  `zk_final_price` 销售价格, `reserve_price` 划线价),
  `publish_info` (`coupon_share_url` 宝贝+券二合一, `click_url` 宝贝推广链接).
- **Error envelope:** `error_response` with `code`/`msg`/`sub_code`/`sub_msg`.

## not_checked (needs an authenticated TOP call or the official signing spec)

- TOP MD5 signing canonicalisation (value URL-encoding, canonical
  composition) — the provided doc does not reproduce the signing algorithm.
- Which optional response fields are actually populated on a live response.

## Adapter mapping decisions (documented, not golden)

- `unit_price` prefers `final_promotion_price`, then `zk_final_price`, then
  `reserve_price`.
- `product_url` prefers `coupon_share_url`, then `click_url`; it is metadata
  only and never used to create a redirect handoff.
