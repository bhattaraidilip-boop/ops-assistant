# ADs Construction — Dashboard Rebuild Brief (2026-11-13)

Staged copy. Intended home: ~/Bhattarai/02_Clients/ADs_Construction/ADsConstruction_DashboardBrief_2026-11-13.md (Mac Mini)

JOB: ADs Construction dashboard rebuild, Wix-analytics feel. Client = ads ONLY. Run the cross-client isolation guard first (client id, business name, domain, phone 720-541-3584) and stop on any mismatch.

## CONTEXT
- Dashboard is hand-built, lives on oracle-excelo, reached at denver.adsconstructionllc.com/dashboard (Wix forwards /dashboard there). Tabs today include Leads, Visitors, Past Inquiries (wix.php).
- Main domain adsconstructionllc.com moved to the client Wix site on Nov 13. Wix certificate still pending. Do NOT touch DNS, Cloudflare, Wix, or the held script ADs_WixMove_EngineDomain.sh.
- Reusable tool: ~/Bhattarai/01_ExceloWeb/Tools/go_client_dashboard.py plus README. Reuse its components, do not duplicate code.
- Secrets rule: never print a value from any env file. Counts, variable names and SET or MISSING only.

## PHASE 1, CHECK ONLY, no changes
Report as a board: Item | Evidence | PASS or FAIL or UNKNOWN
1. Dashboard files, owner, chattr locks, nginx location, login method.
2. Which GA4 property feeds the Visitors tab, and whether the same GA4 ID is present in the published Wix homepage HTML. If the Wix site carries no GA4 ID, say so plainly: homepage and blog visitors stop counting after cutover.
3. Where estimate requests are stored now, and whether Wix form submissions reach the dashboard at all. The Wix API key in /etc/adsconstruction/wix.env is scoped to Blog, Site Media, Members only. State what scope would be needed to read form submissions, and do not attempt it.
4. Real lead count per month for the last 10 months from the leads store, junk and test rows excluded, with the query shown.
5. Screenshot or text outline of the current dashboard on phone width.

Then STOP and give the plan for Phase 2 with files to change, backup path, rollback line. Wait for yes.

## PHASE 2, only after yes
Goal: client opens it on his phone and reads it in 10 seconds. Original design in the AD navy palette, no Wix logos or copied screens.
- Top row, big numbers with a Today / 7 days / 28 days switch: Visitors, Estimate requests, Calls tapped, Google profile searches if available.
- Each number shows change against the prior period.
- Where visitors came from: Google search, Google listing, Facebook, Direct, Other.
- Top pages read, 10 rows.
- Estimate requests list: name, job type, city, date, status mark Called / Booked.
- Leads per month bar chart, last 10 months.
- Keep existing tabs working. Keep login. Mobile first.
- Build so go_client_dashboard.py can apply the same layout to the next client through its config, with a layout key, not a fork.

## Rules
- Backup outside the webroot before any change, test before reload, relock files after verify.
- Final board: Item | Evidence Before | Action | Evidence After | PASS/FAIL, with a phone-width screenshot.
- Save the final report as ~/Bhattarai/02_Clients/ADs_Construction/ADsConstruction_DashboardRebuild_2026-11-13.md
