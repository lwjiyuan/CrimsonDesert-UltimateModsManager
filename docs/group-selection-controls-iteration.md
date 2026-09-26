# Group Selection Controls Iteration

## Why

Applying or disabling every mod in a folder group required changing each mod
individually. This was slow and error-prone for users who organize compatible
mods into version or purpose-based groups.

## What Changed

- Each non-empty folder header now exposes one group-level action.
- The action reads **Select All** unless every mod in that group is enabled.
  It then changes to **Deselect All**.
- Mixed and fully disabled groups become fully enabled with one action.
  Fully enabled groups become fully disabled.
- The action updates only the target group, persists each enabled state, and
  refreshes pending Apply indicators, statistics, the page-level selection
  state, and every group action label.
- Empty groups do not show the action.
- Group labels and counts are synchronized after imports, card rebuilds,
  language changes, global selection changes, individual toggles, and
  drag-and-drop moves.
- Database watcher handling now covers the complete group update operation.

## What This Resolved

Users can prepare an entire mod group for Apply or removal without toggling
every card manually. The dynamic label also makes the next bulk action
explicit and avoids affecting mods in other groups.

## Test Result

Focused regression tests were added for action visibility and labels, signal
delivery, mixed-state selection, full-group deselection, and target-group
isolation. Static diagnostics and diff formatting checks passed. Automated
tests and application builds were not run; user validation is pending.
