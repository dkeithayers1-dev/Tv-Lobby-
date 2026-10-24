const SHIFTS = window.SHIFTS;
// Employees get a view-only grid; the server also rejects their saves.
const IS_ADMIN = window.IS_ADMIN;
const WORK_SHIFTS = Object.keys(SHIFTS).filter((c) => SHIFTS[c].hours > 0);

const monthInput = document.getElementById('month');
const table = document.getElementById('schedule');
const rosterBody = document.getElementById('roster-body');
const statusEl = document.getElementById('status');
const dirtyEl = document.getElementById('dirty');

let state = { month: monthInput.value, days: [], employees: [], shifts: {} };
let dirty = false;

function setDirty(value) {
  dirty = value;
  if (dirtyEl) dirtyEl.textContent = value ? 'Unsaved changes' : '';
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === 'class') node.className = v;
    else node.setAttribute(k, v);
  });
  if (text !== undefined) node.textContent = text;
  return node;
}

function shiftFor(empId, day) {
  return (state.shifts[empId] || {})[String(day)] || '';
}

function setShift(empId, day, code) {
  const row = state.shifts[empId] || (state.shifts[empId] = {});
  if (code) row[String(day)] = code;
  else delete row[String(day)];
}

function shiftSelect(empId, day) {
  const select = el('select', { 'data-emp': empId, 'data-day': day, 'aria-label': `Day ${day}` });
  select.appendChild(el('option', { value: '' }, ''));
  Object.entries(SHIFTS).forEach(([code, s]) => {
    select.appendChild(el('option', { value: code }, s.label));
  });
  select.value = shiftFor(empId, day);
  return select;
}

function render() {
  table.innerHTML = '';

  // Row 1: days of the month
  const thead = el('thead');
  const head = el('tr');
  head.appendChild(el('th', { class: 'name-col' }, 'Employee'));
  state.days.forEach((d) => {
    const th = el('th', { class: ['Sat', 'Sun'].includes(d.weekday) ? 'weekend' : '' });
    th.appendChild(el('div', {}, String(d.day)));
    th.appendChild(el('small', {}, d.weekday));
    head.appendChild(th);
  });
  head.appendChild(el('th', { class: 'total-col' }, 'Hours'));
  thead.appendChild(head);
  table.appendChild(thead);

  // Rows 2+: one per employee, name in the first column
  const tbody = el('tbody');
  state.employees.forEach((emp) => {
    const tr = el('tr');
    const nameCell = el('th', { class: 'name-col', scope: 'row' });
    tr.appendChild(nameCell);
    if (!IS_ADMIN) nameCell.textContent = emp.name;
    else {
      const nameInput = el('input', { value: emp.name, 'data-emp': emp.id, 'aria-label': 'Employee name' });
      nameInput.addEventListener('input', () => {
        emp.name = nameInput.value;
        setDirty(true);
        renderRoster();
      });
      nameCell.appendChild(nameInput);
    }
    state.days.forEach((d) => {
      const code = shiftFor(emp.id, d.day);
      const td = el('td', { class: code ? `shift-${code}` : '' });
      if (IS_ADMIN) td.appendChild(shiftSelect(emp.id, d.day));
      else td.textContent = code ? SHIFTS[code].label : '';
      tr.appendChild(td);
    });
    tr.appendChild(el('td', { class: 'total-col', 'data-emp-total': emp.id }));
    tbody.appendChild(tr);
  });
  tbody.addEventListener('change', (e) => {
    const select = e.target;
    if (select.tagName !== 'SELECT') return;
    setShift(select.dataset.emp, select.dataset.day, select.value);
    select.parentElement.className = select.value ? `shift-${select.value}` : '';
    setDirty(true);
    updateTotals();
  });
  table.appendChild(tbody);

  // Bottom: daily totals
  const tfoot = el('tfoot');
  const rows = [['hours', 'Total hours']].concat(
    WORK_SHIFTS.map((code) => [code, `${SHIFTS[code].label} staff`])
  );
  rows.forEach(([key, label]) => {
    const tr = el('tr', { class: key === 'hours' ? 'hours-row' : '' });
    tr.appendChild(el('th', { class: 'name-col', scope: 'row' }, label));
    state.days.forEach((d) => tr.appendChild(el('td', { 'data-total': `${key}-${d.day}` })));
    tr.appendChild(el('td', { class: 'total-col', 'data-total': `${key}-all` }));
    tfoot.appendChild(tr);
  });
  table.appendChild(tfoot);

  updateTotals();
  renderRoster();
}

function updateTotals() {
  let grandHours = 0;
  const grandCounts = Object.fromEntries(WORK_SHIFTS.map((c) => [c, 0]));

  state.days.forEach((d) => {
    let hours = 0;
    const counts = Object.fromEntries(WORK_SHIFTS.map((c) => [c, 0]));
    state.employees.forEach((emp) => {
      const code = shiftFor(emp.id, d.day);
      if (!code) return;
      hours += SHIFTS[code].hours;
      if (code in counts) counts[code] += 1;
    });
    grandHours += hours;
    table.querySelector(`[data-total="hours-${d.day}"]`).textContent = hours || '';
    WORK_SHIFTS.forEach((c) => {
      grandCounts[c] += counts[c];
      table.querySelector(`[data-total="${c}-${d.day}"]`).textContent = counts[c] || '';
    });
  });
  table.querySelector('[data-total="hours-all"]').textContent = grandHours;
  WORK_SHIFTS.forEach((c) => {
    table.querySelector(`[data-total="${c}-all"]`).textContent = grandCounts[c];
  });

  state.employees.forEach((emp) => {
    const hours = Object.values(state.shifts[emp.id] || {}).reduce((sum, c) => sum + SHIFTS[c].hours, 0);
    table.querySelector(`[data-emp-total="${emp.id}"]`).textContent = hours;
  });
}

function renderRoster() {
  if (!IS_ADMIN) return;
  rosterBody.innerHTML = '';
  state.employees.forEach((emp, i) => {
    const tr = el('tr');
    tr.appendChild(el('td', {}, String(i + 1)));
    tr.appendChild(el('td', {}, emp.name));
    const phoneCell = el('td');
    const phone = el('input', { type: 'tel', value: emp.phone || '', placeholder: '(555) 555-1234' });
    phone.addEventListener('input', () => {
      emp.phone = phone.value;
      setDirty(true);
    });
    phoneCell.appendChild(phone);
    tr.appendChild(phoneCell);
    const actions = el('td');
    const textBtn = el('button', { class: 'small-btn' }, 'Text');
    textBtn.disabled = !window.SMS_CONFIGURED || !emp.phone;
    textBtn.addEventListener('click', () => sendTexts([emp.id]));
    const removeBtn = el('button', { class: 'remove' }, 'Remove');
    removeBtn.addEventListener('click', () => {
      if (!confirm(`Remove ${emp.name} from the roster?`)) return;
      state.employees = state.employees.filter((e) => e.id !== emp.id);
      delete state.shifts[emp.id];
      setDirty(true);
      render();
    });
    actions.appendChild(textBtn);
    actions.appendChild(removeBtn);
    tr.appendChild(actions);
    rosterBody.appendChild(tr);
  });
}

async function load(month) {
  statusEl.textContent = 'Loading...';
  const res = await fetch(`/api/schedule?month=${encodeURIComponent(month)}`);
  const data = await res.json();
  if (!res.ok) {
    statusEl.textContent = data.error;
    return;
  }
  state = data;
  setDirty(false);
  statusEl.textContent = '';
  render();
}

async function save() {
  statusEl.textContent = 'Saving...';
  const res = await fetch('/api/schedule', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ month: state.month, employees: state.employees, shifts: state.shifts }),
  });
  const data = await res.json();
  if (!res.ok) {
    statusEl.textContent = 'Save failed: ' + data.error;
    return false;
  }
  setDirty(false);
  statusEl.textContent = 'Saved.';
  return true;
}

async function sendTexts(employeeIds) {
  if (dirty && !(await save())) return;
  const who = employeeIds.length ? 'this employee' : 'every employee with a phone number';
  if (!confirm(`Text ${who} their ${state.month} schedule?`)) return;
  statusEl.textContent = 'Sending texts...';
  const res = await fetch('/api/schedule/text', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ month: state.month, employee_ids: employeeIds }),
  });
  const data = await res.json();
  if (!res.ok) {
    statusEl.textContent = 'Failed: ' + data.error;
    return;
  }
  const parts = [`Texted ${data.sent.length}.`];
  if (data.skipped.length) parts.push(`Skipped: ${data.skipped.join(', ')}.`);
  if (data.errors.length) parts.push(`Errors: ${data.errors.join('; ')}`);
  statusEl.textContent = parts.join(' ');
}

monthInput.addEventListener('change', () => {
  if (dirty && !confirm('You have unsaved changes. Switch months anyway?')) {
    monthInput.value = state.month;
    return;
  }
  history.replaceState(null, '', `?month=${monthInput.value}`);
  const logoutNext = document.getElementById('logout-next');
  if (logoutNext) logoutNext.value = `/schedule?month=${monthInput.value}`;
  load(monthInput.value);
});
document.getElementById('print-btn').addEventListener('click', () => window.print());
if (IS_ADMIN) {
  document.getElementById('save-btn').addEventListener('click', save);
  document.getElementById('text-all-btn').addEventListener('click', () => sendTexts([]));
  document.getElementById('add-emp-btn').addEventListener('click', () => {
    const used = new Set(state.employees.map((e) => e.id));
    let n = state.employees.length + 1;
    while (used.has(`e${n}`)) n += 1;
    state.employees.push({ id: `e${n}`, name: `Employee ${state.employees.length + 1}`, phone: '' });
    setDirty(true);
    render();
  });
}
window.addEventListener('beforeunload', (e) => {
  if (dirty) e.preventDefault();
});

load(state.month);
