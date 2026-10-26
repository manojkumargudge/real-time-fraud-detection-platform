# Optimistic-lock bug on the case page (found 2026-10-26, fixed in bf63be0)

Found while verifying the analyst dashboard on the live demo database (Step 3). All 11 review
rows written during that verification are in `case_reviews.csv`. They were test reviews (analysts
"alice", "bob", "carol"), and the demo database was then restored from
`backups/fraud-demo-2026-10-26-pre-dashboard.dump` and migrated again, so none of them remain.

## What happened on alert 4705 (rows 8 and 9)

1. Two UI sessions opened alert 4705 at version 1 (status `open`).
2. bob saved `open -> investigating`: version 2 (row 8).
3. alice, whose form was still at version 1, chose **false_positive** and saved.
   - Expected: rejected with the optimistic-lock message, nothing saved.
   - Actual: the save **succeeded** as `investigating -> confirmed_fraud` (row 9, label `fraud`,
     version 3). alice's stale form overwrote bob's state, and the status saved was not the
     one she chose.

## Root cause

- A form submit in Streamlit reruns the whole page script, and the case page read the alert
  again during that rerun.
- bob's save had cleared the shared read cache, so the rerun loaded the alert **at version 2**.
  The page then rebuilt the form from that fresh state:
  - the version sent with the save was the fresh 2, not the 1 alice had looked at, so the
    optimistic-lock check passed;
  - the status options were rebuilt for `investigating`, so alice's choice (`false_positive`,
    an option of the old form) was replaced by the first new option, `confirmed_fraud`.
- The database layer (`data.change_status`, version check under `FOR UPDATE`) was correct; it
  was given the wrong expected version.

## Fix (bf63be0)

- The save runs in the submit button's `on_click` callback (`save_status` in
  `src/dashboard/views/case.py`). Streamlit runs callbacks before the page is re-rendered, so
  the save uses:
  - the version and status the form was rendered with (`st.session_state["case_form_seen"]`,
    written at render time);
  - the selected status from its widget key (`case_to_status`), not a rebuilt widget.
- A stale form now gets `StaleAlertError`, and nothing is saved.

## Regression test

`tests/test_dashboard.py::test_conflict_between_two_ui_sessions_saves_nothing`:
- two AppTest sessions; bob saves through the UI, which clears the cache, then alice submits
  her version-1 form;
- asserts the error names bob and "you had version 1", the alert stays at bob's state, and
  only bob's review exists;
- it fails without the fix and passes with it.

## Live re-check on alert 1502 (rows 10 and 11)

Inside the rebuilt dashboard container, against the demo database:
1. Both sessions opened 1502 at version 1.
2. bob saved `open -> investigating` (version 2, row 10).
3. alice's version-1 form was **rejected**: "alert 1502 was changed by bob at 2026-10-26
   07:47:51 UTC: it is now investigating (version 2, you had version 1). Reload the case to see
   the change; nothing was saved."
4. After reloading, alice saved `investigating -> false_positive` on the current version
   (version 3, row 11).
