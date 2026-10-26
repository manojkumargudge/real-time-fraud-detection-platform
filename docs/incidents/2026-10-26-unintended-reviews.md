# Incident 2026-10-26: unintended case reviews written to the demo DB

Status: **trigger not reproduced; design flaws fixed** on branch `review-save-fix` (see "Fix").
The demo DB was restored to the 16:54 IST backup (see "Restore"). The dashboard stays off on the
demo stack until the fix is merged.

## Summary

- **What happened.** Between 11:30:17 and 11:31:55 UTC (17:00:17–17:01:55 IST), four
  `case_reviews` rows (ids 7–10) were written to the demo DB under analyst "Manojkumar M Hegde".
  Each one flipped the alert to the other label. The analyst did not click Save; they pressed F5
  on Case detail tabs, some of which showed "CONNECTING" after the stacks had been stopped and
  restarted.
- **Trigger: not reproduced.** Refresh, an alert id change, a server restart with the page open,
  F5 while the server was down, and a multi-tab outage of incident length all wrote 0 rows in a
  real browser (see "Reproduction").
- **Most likely trigger.** A submit from a stale browser tab, or a keyboard submit (Enter, or
  Ctrl+Enter in Notes, both submit an `st.form`). The note text in rows 7–10 came from the
  browser, not the server. This is an inference, not a proven cause.
- **Confirmed design flaws.** (d) The expected version was re-read on every render. (e) The
  status dropdown defaulted to a status other than the current one. Together they let **any**
  stray submit silently change a label: it passed the version check and carried a status nobody
  chose.

## Timeline (UTC; IST = UTC+5:30)

| time | event |
|---|---|
| 11:14:24–11:19:11 | Intended reviews, rows 1–6 (analyst in the dashboard) |
| ~11:24 (16:54 IST) | Backup `backups/fraud-demo-2026-10-26-reviewed.dump` taken and verified (case_reviews = 6) |
| 11:25:30–11:25:44 | Demo and test stacks stopped by Claude (`docker compose stop`, at the analyst's request). Dashboard log: `Stopping...` 11:25:30 |
| 11:27:59–11:28:18 | Both stacks started again by Claude ("Step 0: restart" request). The dashboard container started at 11:28:18 and its server was up at 11:28:25 |
| 11:29:07 | Dashboard log shows the Drift page being rendered (`st.components.v1.html` deprecation) |
| 11:30:17–11:31:55 | Rows 7–10 written |

Correction to the reported timeline: the stack was **not** stopped from 16:57 to 17:01 IST. It was
stopped from 16:55:30 to 16:58:18 IST, then running again, dashboard included, from 16:58:25 IST.
Nothing else started containers in that window. Every container's StartedAt matches the restart
commands, and every container shows 0 restarts.

## Evidence

### case_reviews rows created after 11:20 UTC (demo DB, before restore)

| id | alert | from → to | label | version after | created (UTC) | analyst | notes |
|---|---|---|---|---|---|---|---|
| 7 | 4705 | confirmed_fraud → false_positive | not_fraud | 4 | 11:30:17.337740 | Manojkumar M Hegde | Long card history (437 txns); amount within card's usual range |
| 8 | 4705 | false_positive → confirmed_fraud | fraud | 5 | 11:30:35.539109 | Manojkumar M Hegde | Correction: billing address changed (now 330), 17 earlier alerts on card, amount 3.2x user average |
| 9 | 298 | false_positive → confirmed_fraud | fraud | 3 | 11:31:14.518143 | Manojkumar M Hegde | Same gmail and iOS device as earlier transactions, product R used 18 times before; flagged mainly for amount above usual. |
| 10 | 3131 | confirmed_fraud → false_positive | not_fraud | 3 | 11:31:55.137309 | Manojkumar M Hegde | Same recurring product S pattern as 1502; $15, Windows, no email. |

Alert state before the restore: 298 confirmed_fraud v3, 1502 confirmed_fraud v3 (unchanged since
11:19:11), 3131 false_positive v3, 4705 confirmed_fraud v5. No other alert changed.

Observations, from the data only:

- The notes in rows 9 and 10 are byte-identical to the notes of the intended rows 5 (298) and
  2 (3131).
- Rows 7 and 8 match the notes of rows 3 and 4 (4705), except that the trailing period is missing.
- The save callback clears `case_notes` after every successful save (case.py:48). The dashboard
  process restarted at 11:28:25, so no server-side session from before 11:25 existed. The note text
  in rows 7–10 must therefore have been sent by the browser. The reproduction also showed the Notes
  box empty after each save.
- Each replayed `to_status` is the **first** allowed next status (`workflow.next_statuses`) from
  the alert's state at that moment. For 298 and 3131 it differs from the status saved originally
  (open → false_positive / confirmed_fraud).

### Dashboard container log, 11:25–11:35 UTC

```
2026-10-26T11:25:30.658Z   Stopping...
2026-10-26T11:28:25.045Z 2026-10-26 11:28:25.044 Installed websockets 17.3 with uvicorn 0.34.0 can abort the /_stcore/stream handshake when a reverse proxy forwards non-ASCII headers. Upgrade uvicorn to >= 0.52.0, ...
2026-10-26T11:28:25.550Z 2026-10-26 11:28:25.549 Uvicorn server started on 0.0.0.0:8501
2026-10-26T11:28:26.748Z   You can now view your Streamlit app in your browser.
2026-10-26T11:28:26.748Z   Local URL: http://localhost:8501
2026-10-26T11:28:26.748Z   Detected WSL. Using poll-based file watching for better compatibility. ...
2026-10-26T11:29:07.650Z 2026-10-26 11:29:07.649 Please replace `st.components.v1.html` with `st.iframe`.
2026-10-26T11:29:07.650Z `st.components.v1.html` will be removed after 2026-06-01.
```

There are no errors and no per-request logging, so the log cannot show which tab or session wrote
the rows.

## Code checks (src/dashboard/views/case.py, at main f3d2815)

```python
21: FORM_SEEN = "case_form_seen"  # the alert version/status the status form was last rendered with
24: def save_status(alert_id: int) -> None:
26:     seen = st.session_state.get(FORM_SEEN)
27:     if not seen or seen["alert_id"] != alert_id:
28:         st.session_state["case_message"] = ("error", "The form is out of date; reload the case.")
29:         return
30:     to_status = str(st.session_state.get("case_to_status"))
32:         version = ui.repo().change_status(alert_id, seen["version"], to_status, ui.analyst_name(), st.session_state.get("case_notes", ""))
48:         st.session_state["case_notes"] = ""
55: default_id = st.session_state.get("alert_id")
56: alert_id = st.number_input("Alert id", min_value=1, value=default_id, step=1)
60: st.session_state["alert_id"] = int(alert_id)
103: options = workflow.next_statuses(case["status"])
108: with st.form("change_status"):
109:     st.selectbox("New status", options, key="case_to_status", format_func=ui.status_label)
110:     st.text_area("Notes", key="case_notes")
113:     st.form_submit_button("Save", disabled=not analyst, on_click=save_status, args=(int(alert_id),))
116: st.session_state[FORM_SEEN] = {"alert_id": int(alert_id), "version": int(case["version"]), "status": case["status"]}
```

| check | answer | evidence |
|---|---|---|
| a) A reconnecting tab resends the Save "clicked" value | **Not confirmed** | Streamlit 1.66.1 frontend (`submitForm`): the trigger is set, sent once, then `deleteWidgetState(trigger)`. On reconnect, `handleConnectionStateChanged` calls `sendUpdateWidgetsMessage` without a trigger. Reproduction: reconnect after a server stop/start wrote 0 rows (iii, v). The browser did send old values (see Evidence), but the mechanism was not found. |
| b) A save flag in session_state that fires again | **No** | No such flag. A save runs only from the `on_click=save_status` callback (line 113). The only keys written are `alert_id`, `queue_page`, `queue_filter`, `case_message`, `case_notes` and `FORM_SEEN`. |
| c) Alert id / status / save in the URL or elsewhere surviving a refresh | **No** | No `st.query_params` anywhere in `src/dashboard`. `alert_id` lives in session_state only. After F5 the reproduction showed Alert id, Analyst name and Notes empty and Save disabled. |
| d) Expected version read at save time, not render time | **Yes, effectively** | `FORM_SEEN` is overwritten on **every** render (line 116), and `save_status` uses `seen["version"]` (line 32). The expected version is the one from the session's latest render, not from the form the analyst acted on. A replayed or stray submit in a session that has rendered the current version never conflicts. |
| e) Dropdown defaults to a status other than the current one | **Yes** | The selectbox has no placeholder or index (line 109), so it defaults to `options[0]`. `next_statuses` never contains the current status: confirmed_fraud → [false_positive, closed]; false_positive → [confirmed_fraud, closed]. Any submit that carries the default flips the label. This matches every to_status in rows 7–10. |

## Reproduction (test stack only)

Setup:
- Test Postgres (localhost:25432), a separate database `dash_repro` with the schema from
  `PostgresStore` and alerts 1–3 seeded as open v1.
- A dashboard container `fraud-repro-dashboard`, same image (sha256:509d2657...), on network
  `fraud-platform-test_default` only, `DATABASE_URL=.../dash_repro`, published on
  127.0.0.1:18502.
- Driven by headless Microsoft Edge 156 over the Chrome DevTools Protocol, with a separate
  throwaway profile.
- Rows were counted in `dash_repro.case_reviews` before and after each action.
- The demo DB was not touched.

Run 1, one tab:

| action | case_reviews before | after | rows written by the action |
|---|---|---|---|
| 0 open alert 1, analyst set, notes, Save once (deliberate) | 0 | 1 | 1 (deliberate) |
| i F5 refresh | 1 | 1 | **0** |
| ii deliberate save on alert 2, then change Alert id 2 → 3 | 1 | 2 | **0** (+1 is the deliberate save; 2 → 2 across the id change) |
| iii deliberate save, then `docker stop` / `docker start` of the dashboard with the page open (tab showed CONNECTING, then reconnected) | 2 | 3 | **0** (+1 is the deliberate save; 3 → 3 across the restart) |
| iv `docker stop`, F5 while the server was down, `docker start` | 3 | 3 | **0** |

Run 2, closer to the incident (two tabs, ~3-minute outage):

| action | case_reviews before | after | rows written |
|---|---|---|---|
| Tab A saves alert 1, tab B saves alert 2 (deliberate) | 0 | 2 | 2 (deliberate) |
| `docker stop` for 170 s (both tabs CONNECTING), `docker start`, F5 on tab A 2 s after start | 2 | 2 | **0** |
| Tab B auto-reconnects, then F5 | 2 | 2 | **0** |
| F5 on both tabs again | 2 | 2 | **0** |

**Result: not reproduced.** Refresh, alert-id change, a server restart with the page open, F5
while down, and a multi-tab outage of incident length each wrote 0 rows.

Differences from the incident that the reproduction did not cover:
- headless rather than headed Edge;
- tabs open for about 15 minutes with several saves each;
- the analyst's exact interaction sequence (focus or keyboard state at the time of F5);
- Edge features such as form restore and tab discarding.

How the browser came to send old note text (and, for rows 7–8, a slightly older version of it)
together with a submit is **unknown**.

## Restore

See the report for verification. The demo DB was restored from
`backups/fraud-demo-2026-10-26-reviewed.dump` (taken at 16:54 IST), and the api and consumers were
restarted. The dashboard was left stopped.

## Fix (branch `review-save-fix`)

Each change closes one way a stray submit could write a review:

1. **Empty status dropdown.** The dropdown has no default ("Choose a status").
   - Save is disabled, with the reason shown, until an analyst name **and** a status are set.
   - Streamlit 1.66 also drops callbacks of disabled widgets on the server.
   - The save callback (`src/dashboard/case_form.py`) and `data.change_status` both reject a
     save with no status.
2. **One-time form token.** A token is created when the form is first rendered for an alert. It
   records the alert id, version and status seen at that moment, one entry per token rather than
   one slot overwritten on every render.
   - The token names the form's widgets and is carried by the Save callback.
   - `case_reviews.submit_token` stores it under the unique index `ux_case_reviews_submit_token`,
     so the same submit can never write twice, even under concurrency.
   - A save is checked against the token's **seen** version. If the case changed, the save is
     rejected with "This case changed since you loaded it ... Reload the case", and the page also
     warns before the save.
3. **Changing the alert id** discards the old token and the form's contents.
4. **No `st.form` on the case page.** Enter, and Ctrl+Enter in Notes, cannot submit; Save
   submits only on a click.
5. **One log line per save attempt**, for example:
   `case save ok: alert=1 from=open to=investigating analyst='a' token=<hex> version=1->2`
   (rejected attempts are logged at WARNING, with the reason).

Schema: an additive migration in `src/serving/store.py`, adding a nullable `submit_token TEXT`
column and its unique index. Older rows keep NULL. The demo DB is not migrated until the API or a
consumer runs the new code.

Tests (each failed on f3d2815 before the fix; the outputs are in
`docs/evidence/2026-10-26-review-save-fix/`):

- a second submit after a save writes 0 rows;
- the same token resubmitted (including from 4 concurrent threads) writes once;
- a save with a stale seen version is rejected and writes 0 rows;
- a save with no status is rejected;
- changing the alert id after a save, then rerunning, writes 0 rows;
- Save is disabled without a name or without a status;
- the page has no form;
- the migration is additive.

A real-browser check against a throwaway database on the test stack wrote 0 rows for each of:
Enter and Ctrl+Enter in Notes, F5, a server restart with a filled form open, and alert id
changes. It wrote 1 row for the one deliberate Save click.
