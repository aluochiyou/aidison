# Taobao Open Platform — Official API Contract Verification (Product Search only)

Scope per instruction: **only Taobao product search/recommendation**. Official contract is
checked for the product-search method name, permission scope, signing/timestamp, response
format, and error codes. Promotion-link and purchase capabilities are **out of scope**.

Method: read-only fetch of official Taobao Open Platform public pages. No TOP API call that
requires AppKey/Secret, no `.env` read, no docId enumeration.

Access date: **2026-08-08** (UTC, local Asia/Shanghai same day).

## 1. Official URLs used and evidence

| # | Official URL | Page | What it revealed publicly | Evidence |
|---|---|---|---|---|
| 1 | `https://open.taobao.com/` | 淘宝开放平台 portal | Title `<title>淘宝开放平台</title>`, SPA bootstrap `open-protal-ng/0.1.4/web/home.js`, no server-rendered API content | HTTP 200, 2369 B HTML (fetched 2026-08-08) |
| 2 | `https://open.taobao.com/api.htm` | 文档中心 (doc center) | Title `<title>开放平台-文档中心</title>`; doc SPA bundle `miniapp-biz/miniapp-doc/1.2.7/home/app.js`; content handlers are `/handler/api/*`, `/handler/document/*` | HTTP 200, 8303 B HTML (fetched 2026-08-08) |
| 3 | `https://open.taobao.com/doc.htm`, `docV3.htm`, `searchV3.htm`, `https://miniapp.open.taobao.com/docV3.htm` | Doc/search pages (corresponding official pages) | Same doc SPA; doc content not server-rendered. All content is loaded client-side via handlers listed below | HTTP 200 shells, ~7.7–8.3 KB HTML (fetched 2026-08-08) |

Handlers found in the official bundle (`app.js`, v1.2.7, served from `g.alicdn.com` as part of
the official doc center):

- `/handler/document/getDocument`
- `/handler/document/getCatelogConfig`
- `/handler/document/searchSuggest`
- `/handler/api/getApiList`
- `/handler/api/getApiParamList`
- `/handler/api/doApiTest`
- `/handler/tools/searchErrorInfo`, `/handler/tools/errorCodeFeedback`
- OAuth: `https://oauth.taobao.com/authorize?response_type=token...` (referenced in bundle)

All handler fetches returned the official "淘宝开放平台 - 出错了" (error) page
(HTTP 200, error shell), i.e. **content is behind login; no anonymous access**.

## 2. Contract findings vs. official public pages

Legend: `[X]` = not_checked / could not be confirmed from anonymous official public pages.

### 2.1 Product-search method name
- Codebase assumes `taobao.tbk.dg.material.optimal` (`src/aidison/providers/taobao.py:185`,
  with `material_id=13366`, `q`, `page_no`, `page_size`).
- Official public docs for the method are **not visible without login** → `[X]` not_checked.
- Confirmed publicly only that the official doc center hosts API docs under `/handler/api/getApiList`
  etc., but the API-name list is anonymous-login-gated → `[X]`.

### 2.2 Permission scope
- The `taobao.tbk.*` permission/scope is not documented on any anonymous official page → `[X]` not_checked.

### 2.3 Signing / timestamp / request format
- No official signing spec (MD5, canonicalization, timestamp format, `v`, `format`) is exposed
  on anonymous official pages → `[X]` not_checked.
- OAuth authorize endpoint (`https://oauth.taobao.com/authorize?response_type=token...`) is the
  only auth-related URL referenced publicly in the bundle.

### 2.4 Response format
- Existing code expects `{"tbk_dg_material_optimal_response": {"result_list": {"map_data": [...]}}}`,
  and `error_response` in body with `code`/`sub_msg`/`msg` (see `src/aidison/providers/taobao.py:363-336`).
- Official response schema for the search API is **not visible without login** → `[X]` not_checked.

### 2.5 Official error codes
- Error-code lookup exists officially via `/handler/tools/searchErrorInfo`, but requires login → `[X]` not_checked.
- Login-auth error codes are referenced via `https://open.taobao.com/doc.htm?treeId=477&docId=120&docType=1`
  ("登录授权错误码查看"), itself a login-gated doc page → `[X]` not_checked.

## 3. Conclusions

1. The official Taobao Open Platform public web pages do **not** expose the product-search API
   contract (method name, scope, signing, response schema, error codes) to anonymous readers.
   All doc content is client-side rendered behind login; anonymous handler calls return the
   official error shell.
2. **All five scoped contract items are `[X]`/not_checked from official public pages.** The
   codebase's assumptions (`taobao.tbk.dg.material.optimal`, MD5 `secret+canonical+secret`
   signing, `error_response` body shape) are **consistent with no official page found**, but are
   unverified from official sources here.
3. Confirmed publicly from official pages (factual, low-risk): the official domain/site identity
   (`open.taobao.com` = 淘宝开放平台, doc center = 开放平台-文档中心) and the existence of
   login-gated doc/handler infrastructure, including the OAuth authorize endpoint.

## 4. What is left / how to close the gap

- To confirm the contract from official sources, log in to the Taobao Open Platform console
  (or have a credentialed session) and read the `taobao.tbk.dg.material.optimal` doc page and the
  TOP signing spec; or run one authenticated sandbox call.
- Until then, treat all signing/schema/scope/error-code assumptions as `not_checked`.
