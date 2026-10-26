# Evidence: review-save fix (incident 2026-10-26, unintended reviews)

See docs/incidents/2026-10-26-unintended-reviews.md. Everything here ran against the **test**
stack (PostgreSQL on localhost:25432). The demo database was never used.

## Before the fix

The new tests were run against main f3d2815 in a separate worktree:

- `before-fix-unit.txt`: the 6 new AppTest tests; **all failed**.
  - For these runs only, the `save` helper chose the status and clicked Save in the same run,
    because the old page used an `st.form`, and AppTest keeps form values only until the submit.
  - The incident pattern was reproduced:
    - `test_a_second_submit_after_a_save_writes_nothing`: a stray submit after a save wrote a
      second review, confirmed_fraud -> false_positive (1 -> 2 rows).
    - `test_a_save_with_a_stale_seen_version_is_rejected`: alice's save went through after bob's
      (1 -> 2 rows), because the expected version had been refreshed on rerun.
  - The other four failed on the pre-filled status dropdown (flaw e) or on the existing
    `st.form`.
  - `test_changing_the_alert_id_after_a_save_writes_nothing` failed on the pre-filled dropdown.
    Its plain reruns wrote no rows on the old code either.
- `before-fix-integration.txt`: the 5 new data-layer tests; **all failed**. These failures show
  that the old API has no token support, not a reproduced behaviour:
  - the old schema has no token column (`ValueError: substring not found`; the token DDL block
    does not exist yet);
  - `change_status` takes no token argument (`TypeError ... 'submit_token'`).

## After the fix

- `after-fix-unit.txt` and `after-fix-integration.txt`: full suites (counts in the PR).
- `after-fix-browser-check.txt`: headless Edge against the fixed dashboard, run from the working
  tree on 127.0.0.1:18504, using the throwaway database `dash_check` on the test stack (dropped
  afterwards). Rows written:

  | action | rows |
  |---|---|
  | Enter and Ctrl+Enter in Notes | 0 |
  | the deliberate Save click | 1 |
  | Enter or Ctrl+Enter after the save | 0 |
  | F5 | 0 |
  | server restart with a filled form open | 0 |
  | alert id 1 -> 2 -> 3 | 0 |

  The server log had exactly one `case save ok` line.
- **A further restart probe (not in a file).** After a server restart, the browser keeps showing
  the pre-restart form, with Save enabled. Clicking that stale Save wrote 0 rows and produced no
  `case save` log line. The new server session had never rendered that button (it has no widget
  metadata for it), so Streamlit ran no callback. The page then re-rendered with Alert id empty.
