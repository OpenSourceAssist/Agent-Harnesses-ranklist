# How a change gets flagged

The goal is to name real shifts and stay quiet when the numbers only wobble. Every rule below serves one of the two.

## Each session counts once

Turns from one session resemble each other: a long refactoring session reads a lot in every turn, a quick question session reads little. On one developer's history we checked, the intraclass correlation (how much of the spread comes from which session a turn belongs to) was about 0.2 to 0.3 for reads before the first edit, tool calls, and output tokens. Testing turns as if each were independent treats 40 turns of one unusual session as 40 pieces of evidence: in our simulations of histories with no real change, a turn-by-turn test flagged a change in 147 of 150 of them.

So the unit is the session. For each number, each session gets one value: the mean of its turns' values, or for ratios and shares (reads per edit, edits to unread files, failed calls, reasoning) the session's own total ratio. A session that runs across an update counts once on each side. The size of a change, its 20% rule, and its p value all come from these per-session values: the median of sessions for counts and tokens, the mean of sessions for rates and shares.

## The test

Two-sided Mann-Whitney U test (Mann and Whitney, 1947) on the per-session values, with the normal approximation, the correction for ties, and a continuity correction of 0.5. These are the defaults of R's `wilcox.test(exact = FALSE)` and of the asymptotic method in `scipy.stats.mannwhitneyu`; the tests in `skills/evals/regression-finder` check the implementation against published values from both.

Rare events need more care. When one value (usually 0) holds more than half of the sessions, the normal approximation gives p values far too small: 2 sessions with an interrupt out of 20, against none out of 200, gives 0.0000078, while the exact answer is 0.0079. There the script also runs Fisher's exact test (Fisher, 1935) on the sessions away from that value against the sessions at it, and keeps the larger p. It tests such a number only when the busier side has at least 5 sessions away from the common value.

## Versions in time order

Versions sort by number (semantic versioning: `2.1.9` before `2.1.10`, a pre-release such as `0.155.0-alpha.16` before `0.155.0`). A second install can break that order: a desktop app or an SDK script still on an old version runs next to the updated command line, and its version number sorts among versions that ran months earlier. The script keeps the longest run of versions whose median turn time rises with the version number, counted in versions first and sessions second, and leaves the rest out with a note that names them and the dates they ran. Counting sessions first would keep a busy second install and drop the versions of the install that was actually being updated. `--by week` shows every session in time order, second installs included.

## The windows around each update

For each update, the script takes the version just before it and the version just after it, and adds the next nearest versions on each side until a side has at least 30 turns from at least 20 sessions (`--min-turns`, `--min-sessions`, which cannot go below 12). An update without enough on both sides is not tested, and the report lists it with its sessions. Weeks are ISO weeks in UTC and work the same way.

Models run side by side more often than they replace each other, so a model is compared with the model used before it only within two weeks of its first use: the earlier model's sessions from the two weeks before, against the new model's sessions from the two weeks after. When the earlier model still ran 30% or more of those later turns, the report says both were in use at the same time.

Within a window, a number is tested only when each side has at least 12 sessions with a value for it.

## Which tests count

1. **Tests that could never pass are set aside** (Tarone, 1990). With few sessions or rare events, even the most one-sided arrangement of the values cannot give a small p. The script finds, for each test, the smallest p its values could give, and keeps only the tests that could reach significance given how many are kept. The rest are counted in a note and do not dilute the adjustment.
2. **Overlapping windows are one test.** Windows that share versions and point the same way on one number see the same change. They form a group, the group's smallest p stands for it, and the adjustment counts groups, not windows.
3. **The adjustment**: Benjamini-Hochberg (Benjamini and Hochberg, 1995) over the groups. A change is flagged when the adjusted p is under 0.01, the per-session value moved at least 20% (a change from zero counts), and the test and the value point the same way.

## Where a change is placed

A window that borrowed neighbors cannot say which of its versions brought the change, so the report places it within a span: "between X and Y", from the second version of the before side to the last version of the after side. A window that compared one version with the one before it says "after X". Among the windows of a flagged group, the report names the one whose own boundary versions hold the most sessions, then the one with the smaller p, and it never names a version with fewer than 3 sessions on its own. In our simulations of a real change next to a version with 2 sessions, naming the smallest p put the change at the wrong version in 10 of 117 flags; with spans, 0 of 131 fell outside the span shown.

When a flag at one update is followed, within its after side, by the opposite flag on the same number, the versions between differ from both neighbors. The report says that version stands out from the versions around it, once, and headlines neither flag.

## What else changed

For each flagged update the report checks, on the same two sides, whether the model mix changed, whether the work moved between projects or one project holds most of it, whether sessions changed shape (the median length, or the share of one-turn sessions by 25 points or more), whether the share of scripted SDK runs changed by 25 points or more, and whether the two sides were in use at the same time. It names each one it finds in the headline and in its own section, with the rerun that separates it.

## How well it works

In our simulations (histories with 1-turn to 20-turn sessions, each session with its own habits, and edits in about 60% of turns):

- **No real change**: 1 run in 100 flagged anything, with 6 or with 20 sessions per version, and the same with a second install on an old version running scripts the whole time (before the time-order rule, such an install produced a flag in nearly every run).
- **Rare events, 200 sessions against 24**: 1 run in 200 flagged, from a 1-in-1,700 draw; the normal approximation alone flagged 3.
- **A real drop in reads before the first edit**: a 70% drop was found, within the span shown, in about 55 of 100 runs; a 50% drop in about 8 of 100. None was placed outside its span.

Smaller changes need more sessions than most people have. "No change passed the test" often means "not enough evidence yet", not "nothing changed".

## What the numbers cannot tell you

- **Why.** A flag says the numbers moved in a span of updates, not that an update moved them. The kind of work may have changed too; the report names the changes it can see, and it cannot see how hard the tasks were.
- **Small histories.** With few sessions the test cannot reach p under 0.01, whatever the change: 5 sessions against 5 cannot.
- **Retention.** Claude Code deletes transcripts older than `cleanupPeriodDays` (30 days by default), except sessions started or continued in Claude Desktop. Gemini CLI keeps 30 days by default. The report says when the history is shorter than the window you asked for.
- **Codex versions** are recorded once per session file, so a Codex session keeps the version it started with.

## Sources

- Mann, H. B., and Whitney, D. R. (1947). On a test of whether one of two random variables is stochastically larger than the other. *Annals of Mathematical Statistics* 18(1), 50-60.
- Fisher, R. A. (1935). *The Design of Experiments*. Oliver and Boyd. (The exact test for a two-by-two table.)
- Tarone, R. E. (1990). A modified Bonferroni method for discrete data. *Biometrics* 46(2), 515-522.
- Benjamini, Y., and Hochberg, Y. (1995). Controlling the false discovery rate: a practical and powerful approach to multiple testing. *Journal of the Royal Statistical Society, Series B* 57(1), 289-300.
- The analysis this skill repeats on your own history: [anthropics/claude-code#42796](https://github.com/anthropics/claude-code/issues/42796).
