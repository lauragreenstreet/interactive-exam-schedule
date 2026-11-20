"""Interactive prelim schedule feedback tool made with NiceGUI

- Requires an access code to see the schedule and propose changes
- Renders an optimizer solution as a side scrolling calendar of exams
- Allows users to click and move exams, seeing conflict change by date
- Allow users to propose a moving an exam which is saved to a CSV file

"""
import csv
import hmac
import os
import secrets
import threading
from collections import defaultdict
from datetime import datetime
from html import escape
from itertools import combinations
from pathlib import Path

import pandas as pd
from nicegui import app, ui

# ------------------------------------------------------------------ settings
HERE = Path(__file__).resolve().parent
DATA = HERE / 'data'
SLOT1, SLOT2 = 4, 12  # base slots that would receive an additional (B) slot

# Synthetic data shipped with this Space. Each file can be overridden with an
# environment variable of the same name.
SOLN_FILE = Path(os.environ.get('SOLN_FILE', DATA / 'SP27_synthetic_solution.csv'))
COENROLL_FILE = Path(os.environ.get('COENROLL_FILE', DATA / 'SP27_coenroll_synthetic.csv'))
PROPOSALS_FILE = Path(os.environ.get('PROPOSALS_FILE', HERE / 'proposals' / 'SP27_proposals.csv'))

PAD_ROWS = 1  # empty rows below the fullest slot
CONFLICT_ABS, CONFLICT_FRAC = 20, 0.15  # Master sheet conditional-format rule

ACCESS_CODE = os.environ.get('ACCESS_CODE', 'TEST')
# Signs the browser cookie that remembers a correct code. A random secret
# means everyone re-enters the code after a server restart; set
# STORAGE_SECRET to keep people signed in across restarts.
STORAGE_SECRET = os.environ.get('STORAGE_SECRET', secrets.token_hex(32))


# ---------------------------------------------------------------- load data
def slot_label(slot: int, dates: dict, extra: set) -> str:
    """Date label used in the Master header. Extra slots -1/-2 (if the solution
    has any) become '<date> B' and their base slots '<date> A'."""
    if slot == -1:
        return f'{dates[SLOT1]} B'
    if slot == -2:
        return f'{dates[SLOT2]} B'
    if (slot == SLOT1 and -1 in extra) or (slot == SLOT2 and -2 in extra):
        return f'{dates[slot]} A'
    return dates[slot]


output = pd.read_csv(SOLN_FILE, index_col=0)
coenroll = pd.read_csv(COENROLL_FILE, index_col=0)

dates = output[output['Opt_Slot'] >= 0].groupby('Opt_Slot')['Opt_Date'].first().to_dict()
_extra = set(output.loc[output['Opt_Slot'] < 0, 'Opt_Slot'])
output['Label'] = output['Opt_Slot'].apply(lambda s: slot_label(s, dates, _extra))

SLOTS = sorted(output['Label'].unique())

# exam id -> course; a course can have several exams (one per Exam_no)
EXAMS = {f'{r.Course}-{r.Exam_no}': r.Course for r in output.itertuples()}

# Optimizer output: exam id -> slot label. Shared by everyone.
ASSIGNMENT = {f'{r.Course}-{r.Exam_no}': r.Label for r in output.itertuples()}

# Pairwise co-enrollment for the courses in the schedule; the diagonal is the
# course's enrollment. Courses missing from the coenroll file are 'NEW'.
_courses = [c for c in set(EXAMS.values()) if c in coenroll.index]
_sub = coenroll.loc[_courses, _courses]
STUDENTS = {c: int(_sub.at[c, c]) for c in _courses}
COENROLL = {a: {b: int(n) for b, n in _sub.loc[a].items() if n and b != a}
            for a in _courses}

BASE_ROWS = output.groupby('Label').size().max() + PAD_ROWS


# ------------------------------------------------------------ proposals CSV
PROPOSAL_FIELDS = ['proposal_id', 'submitted', 'name', 'view', 'exam', 'course',
                   'exam_no', 'from_slot', 'to_slot', 'conflict_change']
_proposals_lock = threading.Lock()


def load_proposals() -> list[dict]:
    if not PROPOSALS_FILE.exists():
        return []
    with open(PROPOSALS_FILE, newline='') as f:
        return list(csv.DictReader(f))


def save_proposal(name: str, view: str, moves: dict, conflict_change: int) -> int:
    """Append one row per moved exam; returns the new proposal id."""
    with _proposals_lock:
        existing = load_proposals()
        pid = max((int(r['proposal_id']) for r in existing), default=0) + 1
        PROPOSALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        new_file = not PROPOSALS_FILE.exists()
        with open(PROPOSALS_FILE, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=PROPOSAL_FIELDS)
            if new_file:
                w.writeheader()
            stamp = datetime.now().isoformat(timespec='seconds')
            for eid, (old, new) in moves.items():
                w.writerow({'proposal_id': pid, 'submitted': stamp, 'name': name,
                            'view': view, 'exam': eid, 'course': EXAMS[eid],
                            'exam_no': eid.rsplit('-', 1)[1], 'from_slot': old,
                            'to_slot': new, 'conflict_change': conflict_change})
    return pid


def proposed_assignment() -> tuple[dict, dict]:
    """ASSIGNMENT with all submitted moves applied in order"""
    assign, by_exam = dict(ASSIGNMENT), {}
    for r in load_proposals():
        if r['exam'] in assign and r['to_slot'] in SLOTS:
            assign[r['exam']] = r['to_slot']
            by_exam[r['exam']] = r
    return assign, {e: r for e, r in by_exam.items() if assign[e] != ASSIGNMENT[e]}


# ---------------------------------------------------------- conflict logic
def shared(a: str, b: str) -> int:
    """Students enrolled in both exams' courses (0 for the same course)."""
    return COENROLL.get(EXAMS[a], {}).get(EXAMS[b], 0)


def clashes(eid: str, others: list[str]) -> dict[str, int]:
    """Exams in `others` (one slot's exams) sharing students with `eid`."""
    return {o: n for o in others if o != eid and (n := shared(eid, o))}


def conflict_count(eid: str, others: list[str]) -> int:
    return sum(clashes(eid, others).values())


def total_conflicts(assign: dict) -> int:
    by_slot = defaultdict(list)
    for eid, s in assign.items():
        by_slot[s].append(eid)
    return sum(shared(a, b) for eids in by_slot.values()
               for a, b in combinations(eids, 2))


def flagged(conflicts: int, students: int) -> bool:
    return conflicts > CONFLICT_ABS or (students > 0 and conflicts / students > CONFLICT_FRAC)


def exam_name(eid: str) -> str:
    return f'{EXAMS[eid]} (exam {eid.rsplit("-", 1)[1]})'


# ------------------------------------------------------------------- page
VIEWS = {'initial': 'Initial schedule', 'proposed': 'With proposed changes'}

# The schedule is drawn as one HTML table (rather than one NiceGUI element per
# cell) so a click sends a single update to the browser. Later rules win when
# a cell has several backgrounds: column tint < exam highlight / red flag.
GRID_CSS = '''
.sched { border-collapse: separate; border-spacing: 0; font-size: .875rem;
         border-left: 1px solid #d1d5db; border-top: 1px solid #d1d5db; }
.sched td, .sched th { padding: 2px 8px; white-space: nowrap; font-weight: 400;
         border-right: 1px solid #d1d5db; border-bottom: 1px solid #d1d5db; }
.sched th, .sched .sum { background: #f3f4f6; }
.sched th, .sched .rn { font-weight: 700; text-align: center; }
.sched .ed { border-right: 3px double #9ca3af; }  /* closes each day's columns */
.sched .r { text-align: right; }
.sched .c { text-align: center; }
.sched .ex, .sched.picking [data-s] { cursor: pointer; }
.sched .dl { display: block; font-size: .75rem; font-weight: 400; }
.sched .t0 { background: #dcfce7; }
.sched .t1 { background: #fef2f2; }
.sched .t2 { background: #fee2e2; }
.sched .pr { background: #ddd6fe; }
.sched .mv { background: #fde68a; }
.sched .se { background: #2563eb; color: #fff; }
.sched .fl { background: #FFC7CE; color: #9C0006; }
'''

# Clicks anywhere in the table: report the exam (if any) and slot index of the
# cell under the pointer; cells outside a slot column have no data-s.
GRID_CLICK_JS = '''(e) => {
    const c = e.target.closest('[data-s]');
    if (c) emit({exam: c.dataset.e || null, slot: Number(c.dataset.s)});
}'''


# Hover text for table cells with a data-tip attribute. One shared box with
# fixed positioning, so it isn't clipped by the table's scroll area and shows
# immediately (the browser's own title tooltips wait about a second).
TOOLTIP_HTML = '''
<div id="sched-tip" style="position: fixed; display: none; z-index: 1000; pointer-events: none;
     background: #374151; color: #fff; font-size: .8rem; padding: 4px 8px; border-radius: 4px;
     white-space: pre-line; max-width: 320px;"></div>
<script>
(() => {
  const tip = () => document.getElementById('sched-tip');
  const hide = () => { tip().style.display = 'none'; };
  document.addEventListener('mouseover', (e) => {
    const cell = e.target.closest('.sched [data-tip]');
    if (!cell) return hide();
    const t = tip();
    t.textContent = cell.dataset.tip;
    t.style.display = 'block';
    const r = cell.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = Math.max(4, Math.min(r.left, innerWidth - w - 4)) + 'px';
    t.style.top = (r.bottom + h + 4 > innerHeight ? r.top - h - 4 : r.bottom + 4) + 'px';
  });
  document.addEventListener('scroll', hide, true);
  document.addEventListener('click', hide);
})();
</script>
'''


def td(text='', cls='', slot=None, exam=None, tip=None, tag='td', span=None) -> str:
    attrs = f' class="{cls}"' if cls else ''
    if slot is not None:
        attrs += f' data-s="{slot}"'
    if exam is not None:
        attrs += f' data-e="{escape(exam)}"'
    if tip:
        attrs += f' data-tip="{escape(tip)}"'
    if span:
        attrs += f' colspan="{span}"'
    return f'<{tag}{attrs}>{escape(str(text))}</{tag}>'


def has_access() -> bool:
    return hmac.compare_digest(app.storage.user.get('access_code', ''), ACCESS_CODE)


@ui.page('/login')
def login():
    if has_access():
        ui.navigate.to('/')
        return

    def check():
        if hmac.compare_digest(code.value.strip(), ACCESS_CODE):
            app.storage.user['access_code'] = ACCESS_CODE
            ui.navigate.to('/')
        else:
            code.value = ''
            ui.notify('Incorrect access code', type='negative')

    with ui.card().classes('absolute-center items-stretch w-80'):
        ui.label('Prelim schedule feedback').classes('text-lg font-semibold')
        code = ui.input('Access code', password=True).on('keydown.enter', check)
        ui.button('Enter', on_click=check)


@ui.page('/')
def index():
    if not has_access():
        ui.navigate.to('/login')
        return

    # Per-browser state. `moves` are this user's unsent edits, layered on top
    # of whichever view is showing; the view defaults to the initial schedule.
    state = {'view': 'initial', 'moves': {}, 'selected': None}

    def base() -> tuple[dict, dict]:
        """Schedule for the current view and the proposal rows behind it."""
        if state['view'] == 'proposed':
            return proposed_assignment()
        return dict(ASSIGNMENT), {}

    def trial_from(assign: dict) -> dict:
        return {**assign, **state['moves']}

    def changes(assign: dict) -> dict:
        return {e: (assign[e], s) for e, s in state['moves'].items() if s != assign[e]}

    def refresh():
        table.content = grid_html()
        panel.refresh()

    def on_grid_click(e):
        exam, slot = e.args.get('exam'), e.args.get('slot')
        if exam in EXAMS:
            select(exam)
        elif isinstance(slot, int) and 0 <= slot < len(SLOTS):
            move_to(SLOTS[slot])

    def select(eid):
        state['selected'] = None if state['selected'] == eid else eid
        refresh()

    def move_to(slot):
        eid = state['selected']
        if not eid:
            return
        assign, _ = base()
        if slot == assign[eid]:
            state['moves'].pop(eid, None)  # moved back to where it started
        else:
            state['moves'][eid] = slot
        state['selected'] = None
        refresh()

    def set_view(e):
        state['view'] = e.value
        state['selected'] = None
        refresh()

    def reset():
        state['moves'] = {}
        state['selected'] = None
        refresh()

    def submit():
        if not name.value:
            ui.notify('Enter your NetID before sending the proposal', type='warning')
            return
        assign, _ = base()
        moves = changes(assign)
        if not moves:
            return
        delta = total_conflicts(trial_from(assign)) - total_conflicts(assign)
        pid = save_proposal(name.value, state['view'], moves, delta)
        ui.notify(f'Proposal {pid} saved ({len(moves)} moves)', type='positive')
        reset()

    def grid_html() -> str:
        assign, proposed_rows = base()
        trial, sel = trial_from(assign), state['selected']
        by_slot = defaultdict(list)
        for eid, s in trial.items():
            by_slot[s].append(eid)
        for eids in by_slot.values():
            eids.sort()
        rows = max(BASE_ROWS, max(len(v) for v in by_slot.values()) + PAD_ROWS)
        cur = conflict_count(sel, by_slot[trial[sel]]) if sel else 0

        # Per-slot what-if delta and column tint while an exam is selected
        delta, tint = {}, {}
        for s in SLOTS:
            if sel and s != trial[sel]:
                delta[s] = conflict_count(sel, by_slot[s]) - cur
                tint[s] = ('t0' if delta[s] < 0 else '' if delta[s] == 0 else
                           't1' if delta[s] <= 10 else 't2')

        out = [f'<table class="sched{" picking" if sel else ""}">']

        # Top header row: each date spans its Course / Enrollment / Conflicts columns
        out.append('<tr>' + td(tag='th', cls='ed'))
        for i, s in enumerate(SLOTS):
            dl = f'<span class="dl">{delta[s]:+d}</span>' if s in delta else ''
            out.append(f'<th class="ed {tint.get(s, "")}" colspan="3" data-s="{i}">'
                       f'{escape(s)}{dl}</th>')
        out.append('</tr>')

        # Second header row: column labels within each date
        out.append('<tr>' + td(tag='th', cls='ed'))
        for i, s in enumerate(SLOTS):
            t = tint.get(s, '')
            out.append(td('Course', t, i, tag='th') + td('Enrollment', t, i, tag='th')
                       + td('Conflicts', f'ed {t}', i, tag='th'))
        out.append('</tr>')

        sums = {s: [0, 0] for s in SLOTS}
        for row in range(rows):
            out.append('<tr>' + td(row, 'rn ed'))
            for i, s in enumerate(SLOTS):
                t = tint.get(s, '')
                if row >= len(by_slot[s]):
                    out.append(td('', t, i) * 2 + td('', f'ed {t}', i))
                    continue
                eid = by_slot[s][row]
                course = EXAMS[eid]
                hl = ('se' if eid == sel else 'mv' if eid in state['moves'] else
                      'pr' if eid in proposed_rows else '')
                tip = None
                if eid in proposed_rows:
                    r = proposed_rows[eid]
                    tip = (f'Proposal {r["proposal_id"]} by {r["name"]}: '
                           f'{ASSIGNMENT[eid]} → {r["to_slot"]}')
                out.append(td(course, f'ex {t} {hl}', i, exam=eid, tip=tip))
                if course not in STUDENTS:
                    out.append(td(0, f'r {t}', i) + td('NEW', f'c ed {t}', i))
                    continue
                students = STUDENTS[course]
                found = clashes(eid, by_slot[s])
                n = sum(found.values())
                sums[s][0] += students
                sums[s][1] += n
                tip = '\n'.join(f'{EXAMS[o]}: {k}' for o, k in
                                sorted(found.items(), key=lambda x: -x[1]))
                out.append(td(students, f'r {t}', i)
                           + td(n, f'r ed {t}' + (' fl' if flagged(n, students) else ''),
                                i, tip=tip))
            out.append('</tr>')

        # Sum row (conflicts halved: each pair is counted from both sides)
        out.append('<tr>' + td(rows + 1, 'rn ed'))
        for i, s in enumerate(SLOTS):
            t = tint.get(s, '')
            out.append(td('Sum', f'sum {t}', i) + td(sums[s][0], f'sum r {t}', i)
                       + td(f'{sums[s][1] / 2:g}', f'sum r ed {t}', i))
        out.append('</tr></table>')
        return ''.join(out)

    @ui.refreshable
    def panel():
        """Conflict totals, instructions and this user's pending moves"""
        assign, proposed_rows = base()
        trial, sel = trial_from(assign), state['selected']
        moves = changes(assign)
        send.set_enabled(bool(moves))
        initial, current, after = (total_conflicts(ASSIGNMENT), total_conflicts(assign),
                                   total_conflicts(trial))
        if state['view'] == 'proposed':
            n_props = len({r['proposal_id'] for r in load_proposals()})
            totals = (f'Total conflicts: {initial} initially, {current} with {n_props} '
                      f'submitted proposal(s), {after} with your changes')
            hint = f'{len(proposed_rows)} exam(s) moved by proposals (purple). '
        else:
            totals = f'Total conflicts: {current} now, {after} with your changes'
            hint = ''
        if sel:
            hint += f'Moving {exam_name(sel)}, {STUDENTS.get(EXAMS[sel], "NEW")} students.'
        with ui.column().classes('gap-0'):
            ui.label(totals).classes('font-semibold')
            if hint:
                ui.label(hint).classes('text-sm')
            if moves:
                ui.label('Your changes: ' + '; '.join(
                    f'{exam_name(e)}: {old} → {new}' for e, (old, new) in moves.items()
                )).classes('text-sm')

    ui.add_css(GRID_CSS)
    ui.add_body_html(TOOLTIP_HTML)
    with ui.column().classes('w-full px-4 py-2 gap-2'):
        # One toolbar row (wraps on narrow screens) to leave room for the schedule
        with ui.row().classes('w-full items-center gap-x-6 gap-y-2'):
            ui.toggle(VIEWS, value='initial', on_change=set_view).props('dense')
            panel_box = ui.element('div').classes('flex-1 min-w-64')
            with ui.row().classes('items-center gap-2'):
                name = ui.input('NetID').props('dense outlined').classes('w-32')
                send = ui.button('Send proposal', on_click=submit)
                ui.button('Undo all', on_click=reset).props('flat')
        with panel_box:
            panel()
        table = ui.html(grid_html()).classes('w-full overflow-auto')
        table.on('click', on_grid_click, js_handler=GRID_CLICK_JS)


# Local defaults; the hosted demo sets HOST=0.0.0.0, PORT=7860, RELOAD=0
ui.run(title='Prelim schedule feedback', storage_secret=STORAGE_SECRET,
       host=os.environ.get('HOST'), port=int(os.environ.get('PORT', 8080)),
       reload=os.environ.get('RELOAD', '1') == '1',
       # When shown inside another site (e.g. the Hugging Face Space page), the
       # login cookie must be allowed in a cross-site iframe
       session_middleware_kwargs=({'same_site': 'none', 'https_only': True}
                                  if os.environ.get('EMBEDDED') == '1' else None))
