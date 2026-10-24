/**
 * Employee Schedule: Twilio texting + new-month tabs for Google Sheets.
 *
 * Paste this whole file into Extensions > Apps Script in the "Employee
 * Schedule" spreadsheet, save, and reload the sheet. A "Schedule" menu
 * appears for editors (employees with view-only access never see it).
 *
 * Layout this expects on each month tab (e.g. "Nov 2026"):
 *   Row 1      : "Employee", one column per day, then "Hours"
 *   Rows 2..   : employee name in column A, shift in each day column
 *   "Total hours" in column A marks the first totals row (end of employees)
 *
 * Phone numbers live in a separate, private spreadsheet so employees who
 * can view the schedule can't see each other's numbers. Twilio credentials
 * are stored in Script Properties (set via the menu), never in this code.
 */

// The private "Employee Phones (private)" spreadsheet: column A = name, B = mobile.
var PHONES_SPREADSHEET_ID = '1qGeMJlRNzcFCyvBavZ345fHbgO32LXTX8eQjLn65qR0';

var SHIFTS = ['7a-3p', '3p-11p', '11p-7a'];
var OFF = 'Off';
var HOURS_PER_SHIFT = 8;
var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
var DAY_ABBR = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Schedule')
    .addItem('Text everyone this month\'s schedule', 'textEveryone')
    .addItem('Text selected employee(s) only', 'textSelected')
    .addItem('Send myself a test text', 'sendTestText')
    .addSeparator()
    .addItem('Create next month\'s tab', 'createNextMonth')
    .addSeparator()
    .addItem('Set up Twilio...', 'setUpTwilio')
    .addToUi();
}

// ---------- Menu actions ----------

function textEveryone() {
  sendSchedules_(null);
}

function textSelected() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var layout = readLayout_(sheet);
  var rows = {};
  var ranges = sheet.getActiveRangeList() ? sheet.getActiveRangeList().getRanges() : [];
  ranges.forEach(function (r) {
    for (var row = r.getRow(); row < r.getRow() + r.getNumRows(); row++) {
      if (row >= 2 && row < layout.totalsRow) rows[row] = true;
    }
  });
  if (!Object.keys(rows).length) {
    SpreadsheetApp.getUi().alert('Click a cell in the row of each employee you want to text, then try again.');
    return;
  }
  sendSchedules_(rows);
}

function sendTestText() {
  var ui = SpreadsheetApp.getUi();
  var res = ui.prompt('Send a test text', 'Your mobile number:', ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;
  try {
    sendSms_(res.getResponseText(), 'Test from the Employee Schedule sheet. Texting works!');
    ui.alert('Sent! Check your phone.');
  } catch (e) {
    ui.alert('Could not send: ' + e.message);
  }
}

function setUpTwilio() {
  var ui = SpreadsheetApp.getUi();
  var props = PropertiesService.getScriptProperties();
  var fields = [
    ['TWILIO_ACCOUNT_SID', 'Twilio Account SID (starts with AC):'],
    ['TWILIO_AUTH_TOKEN', 'Twilio Auth Token:'],
    ['TWILIO_FROM_NUMBER', 'Your Twilio phone number (e.g. +14805551234):'],
  ];
  for (var i = 0; i < fields.length; i++) {
    var current = props.getProperty(fields[i][0]);
    var hint = current ? '\n(Leave blank to keep the current value.)' : '';
    var res = ui.prompt('Set up Twilio (' + (i + 1) + ' of 3)', fields[i][1] + hint, ui.ButtonSet.OK_CANCEL);
    if (res.getSelectedButton() !== ui.Button.OK) return;
    var value = res.getResponseText().trim();
    if (value) props.setProperty(fields[i][0], value);
    else if (!current) {
      ui.alert('That one is required. Run Set up Twilio again when you have it.');
      return;
    }
  }
  ui.alert('Twilio is set up. Try "Send myself a test text" next.');
}

function createNextMonth() {
  var ss = SpreadsheetApp.getActive();
  var source = ss.getActiveSheet();
  var month = parseMonth_(source.getName());
  var year = month.year;
  var mon = month.month + 1;
  if (mon > 11) { mon = 0; year += 1; }
  var name = MONTHS[mon] + ' ' + year;
  if (ss.getSheetByName(name)) {
    SpreadsheetApp.getUi().alert('A "' + name + '" tab already exists.');
    return;
  }
  var names = readLayout_(source).employees.map(function (e) { return [e.name]; });
  var sheet = ss.insertSheet(name, ss.getNumSheets());
  buildMonthTab_(sheet, year, mon, names);
  ss.setActiveSheet(sheet);
  SpreadsheetApp.getUi().alert('Created "' + name + '" with the same ' + names.length + ' employees.');
}

// ---------- Sending ----------

function sendSchedules_(onlyRows) {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSheet();
  var month = parseMonth_(sheet.getName());
  var layout = readLayout_(sheet);
  var phones = loadPhones_();
  var who = onlyRows ? Object.keys(onlyRows).length + ' selected employee(s)' : 'every employee with a phone number';
  var ok = ui.alert('Text schedules',
    'Text ' + who + ' their ' + MONTH_NAMES[month.month] + ' ' + month.year + ' schedule?',
    ui.ButtonSet.YES_NO);
  if (ok !== ui.Button.YES) return;

  var link = SpreadsheetApp.getActive().getUrl();
  var sent = [], skipped = [], errors = [];
  layout.employees.forEach(function (emp) {
    if (onlyRows && !onlyRows[emp.row]) return;
    var phone = phones[normalizeName_(emp.name)];
    if (!phone) {
      skipped.push(emp.name);
      return;
    }
    try {
      sendSms_(phone, buildMessage_(emp, month, link));
      sent.push(emp.name);
    } catch (e) {
      errors.push(emp.name + ': ' + e.message);
    }
  });

  var report = 'Texted ' + sent.length + ' employee(s).';
  if (skipped.length) report += '\n\nNo phone number found for: ' + skipped.join(', ') +
    '\n(Add them in "Employee Phones (private)", spelled exactly as on the schedule.)';
  if (errors.length) report += '\n\nErrors:\n' + errors.join('\n');
  ui.alert(report);
}

function buildMessage_(emp, month, link) {
  var lines = [];
  var hours = 0;
  emp.shifts.forEach(function (shift, i) {
    if (SHIFTS.indexOf(shift) === -1) return;
    lines.push((month.month + 1) + '/' + (i + 1) + ' ' + shift);
    hours += HOURS_PER_SHIFT;
  });
  var header = emp.name + ', your ' + MONTH_NAMES[month.month] + ' ' + month.year + ' schedule';
  var body = lines.length ? header + ' (' + hours + ' hrs):\n' + lines.join('\n') : header + ': no shifts scheduled.';
  if (link) body += '\nFull schedule: ' + link;
  return body;
}

function sendSms_(to, body) {
  var props = PropertiesService.getScriptProperties();
  var sid = props.getProperty('TWILIO_ACCOUNT_SID');
  var token = props.getProperty('TWILIO_AUTH_TOKEN');
  var from = props.getProperty('TWILIO_FROM_NUMBER');
  if (!sid || !token || !from) throw new Error('Twilio is not set up yet. Use Schedule > Set up Twilio first.');

  var res = UrlFetchApp.fetch('https://api.twilio.com/2010-04-01/Accounts/' + sid + '/Messages.json', {
    method: 'post',
    payload: { To: normalizePhone_(to), From: from, Body: body },
    headers: { Authorization: 'Basic ' + Utilities.base64Encode(sid + ':' + token) },
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() >= 300) {
    var msg = res.getContentText();
    try { msg = JSON.parse(msg).message; } catch (e) { /* keep raw text */ }
    throw new Error(msg);
  }
}

// ---------- Reading the sheet ----------

function readLayout_(sheet) {
  var values = sheet.getDataRange().getDisplayValues();
  var header = values[0];
  var hoursCol = header.indexOf('Hours');
  var lastDayCol = hoursCol > 0 ? hoursCol - 1 : header.length - 1;
  var totalsRow = values.length + 1;
  for (var r = 1; r < values.length; r++) {
    if (String(values[r][0]).trim() === 'Total hours') { totalsRow = r + 1; break; }
  }
  var employees = [];
  for (var row = 2; row < totalsRow; row++) {
    var line = values[row - 1];
    var name = String(line[0]).trim();
    if (!name) continue;
    employees.push({ row: row, name: name, shifts: line.slice(1, lastDayCol + 1) });
  }
  return { employees: employees, totalsRow: totalsRow, days: lastDayCol };
}

function loadPhones_() {
  var values = SpreadsheetApp.openById(PHONES_SPREADSHEET_ID).getSheets()[0].getDataRange().getDisplayValues();
  var phones = {};
  for (var r = 1; r < values.length; r++) {
    var name = normalizeName_(values[r][0]);
    var phone = String(values[r][1]).trim();
    if (name && phone) phones[name] = phone;
  }
  return phones;
}

function parseMonth_(tabName) {
  var m = String(tabName).trim().match(/^([A-Za-z]{3})[a-z]*\s+(\d{4})$/);
  var idx = m ? MONTHS.indexOf(m[1].charAt(0).toUpperCase() + m[1].slice(1, 3).toLowerCase()) : -1;
  if (idx === -1) {
    throw new Error('Open a month tab first (named like "Nov 2026"). This tab is "' + tabName + '".');
  }
  return { year: Number(m[2]), month: idx };
}

function normalizeName_(name) {
  return String(name || '').trim().replace(/\s+/g, ' ').toLowerCase();
}

function normalizePhone_(phone) {
  phone = String(phone || '').trim();
  var digits = phone.replace(/\D/g, '');
  if (phone.charAt(0) === '+') return '+' + digits;
  if (digits.length === 10) return '+1' + digits;
  if (digits.length === 11 && digits.charAt(0) === '1') return '+' + digits;
  throw new Error('Can\'t read phone number "' + phone + '"');
}

// ---------- Building a month tab ----------

function columnLetter_(n) {
  var s = '';
  while (n > 0) {
    var m = (n - 1) % 26;
    s = String.fromCharCode(65 + m) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

function buildMonthTab_(sheet, year, mon, names) {
  var days = new Date(year, mon + 1, 0).getDate();
  var nEmp = names.length;
  var firstRow = 2, lastRow = 1 + nEmp, totalsRow = lastRow + 1;
  var lastDayCol = 1 + days, hoursCol = lastDayCol + 1;
  var lastDay = columnLetter_(lastDayCol);

  sheet.clear();
  if (sheet.getMaxColumns() < hoursCol) sheet.insertColumnsAfter(sheet.getMaxColumns(), hoursCol - sheet.getMaxColumns());
  if (sheet.getMaxRows() < totalsRow + 3) sheet.insertRowsAfter(sheet.getMaxRows(), totalsRow + 3 - sheet.getMaxRows());

  var header = ['Employee'];
  var weekendCols = [];
  for (var d = 1; d <= days; d++) {
    var dow = new Date(year, mon, d).getDay();
    header.push(DAY_ABBR[dow] + '\n' + (mon + 1) + '/' + d);
    if (dow === 0 || dow === 6) weekendCols.push(d + 1);
  }
  header.push('Hours');
  sheet.getRange(1, 1, 1, hoursCol).setValues([header]);
  sheet.getRange(firstRow, 1, nEmp, 1).setValues(names);

  var hourFormulas = [];
  for (var r = firstRow; r <= lastRow; r++) {
    var span = 'B' + r + ':' + lastDay + r;
    hourFormulas.push(['=' + HOURS_PER_SHIFT + '*(COUNTA(' + span + ')-COUNTIF(' + span + ',"' + OFF + '"))']);
  }
  sheet.getRange(firstRow, hoursCol, nEmp, 1).setFormulas(hourFormulas);

  var labels = ['Total hours'].concat(SHIFTS.map(function (s) { return s + ' staff'; }));
  var totals = labels.map(function (label, i) {
    var row = [label];
    for (var c = 2; c <= lastDayCol; c++) {
      var col = columnLetter_(c);
      var span = col + firstRow + ':' + col + lastRow;
      row.push(i === 0
        ? '=' + HOURS_PER_SHIFT + '*(COUNTA(' + span + ')-COUNTIF(' + span + ',"' + OFF + '"))'
        : '=COUNTIF(' + span + ',"' + SHIFTS[i - 1] + '")');
    }
    var r = totalsRow + i;
    row.push('=SUM(B' + r + ':' + lastDay + r + ')');
    return row;
  });
  sheet.getRange(totalsRow, 1, totals.length, hoursCol).setFormulas(totals);

  // Formatting to match the original tab
  var all = sheet.getRange(1, 1, totalsRow + 3, hoursCol);
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(1);
  sheet.setColumnWidth(1, 160);
  sheet.setColumnWidths(2, days, 62);
  sheet.setColumnWidth(hoursCol, 60);
  sheet.setRowHeight(1, 40);
  sheet.getRange(1, 2, totalsRow + 3, hoursCol - 1).setHorizontalAlignment('center').setVerticalAlignment('middle').setFontSize(9);
  sheet.getRange(1, 1, 1, hoursCol).setBackground('#1f2937').setFontColor('#ffffff').setFontWeight('bold').setWrap(true)
    .setHorizontalAlignment('center').setVerticalAlignment('middle');
  weekendCols.forEach(function (c) { sheet.getRange(1, c).setBackground('#4b5563'); });
  sheet.getRange(firstRow, 1, nEmp, 1).setFontWeight('bold');
  sheet.getRange(firstRow, hoursCol, nEmp, 1).setBackground('#f3f4f6').setFontWeight('bold');
  sheet.getRange(totalsRow, 1, 4, hoursCol).setBackground('#f3f4f6').setFontWeight('bold');
  sheet.getRange(totalsRow, 1, 1, hoursCol).setBackground('#dcfce7').setFontSize(10);
  all.setBorder(true, true, true, true, true, true, '#d1d5db', SpreadsheetApp.BorderStyle.SOLID);
  sheet.getRange(totalsRow, 1, 1, hoursCol).setBorder(true, null, null, null, null, null, '#111827', SpreadsheetApp.BorderStyle.SOLID_THICK);

  var grid = sheet.getRange(firstRow, 2, nEmp, days);
  grid.setDataValidation(SpreadsheetApp.newDataValidation()
    .requireValueInList(SHIFTS.concat([OFF]), true)
    .setAllowInvalid(false)
    .setHelpText('Pick a shift: ' + SHIFTS.join(', ') + ' or ' + OFF)
    .build());
  var colors = { '7a-3p': ['#dbeafe', '#1e3a8a'], '3p-11p': ['#fef3c7', '#78350f'], '11p-7a': ['#ede9fe', '#4c1d95'], 'Off': ['#f3f4f6', '#6b7280'] };
  sheet.setConditionalFormatRules(Object.keys(colors).map(function (v) {
    return SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(v)
      .setBackground(colors[v][0]).setFontColor(colors[v][1]).setRanges([grid]).build();
  }));
  sheet.getRange(totalsRow, 1, 4, hoursCol).protect().setDescription('Daily totals (formulas)').setWarningOnly(true);
  sheet.getRange(1, hoursCol, totalsRow - 1, 1).protect().setDescription('Employee hours (formulas)').setWarningOnly(true);
}
