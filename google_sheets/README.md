# Employee Schedule in Google Sheets

A Google Sheets version of the schedule, for when you'd rather not host
the web app. Two spreadsheets:

- **Employee Schedule**: one tab per month (e.g. `Nov 2026`). Row 1 has
  the days of the month, column A (from row 2) the employee names, and
  each cell a dropdown: 7a-3p, 3p-11p, 11p-7a or Off. The **Hours** column
  totals each employee's month, and the bottom rows total scheduled hours
  and headcount per shift for each day (8 hours per shift). Share it with
  employees as **Viewer** and with managers as **Editor**.
- **Employee Phones (private)**: employee name in column A (spelled
  exactly as on the schedule) and mobile number in column B. Don't share
  this one. Anyone who can view a spreadsheet can see all of its tabs,
  which is why the phone numbers live in their own file.

## Installing the texting script

1. Open **Employee Schedule** and choose **Extensions > Apps Script**.
2. Delete what's in `Code.gs`, paste in all of [`ScheduleTexting.gs`](ScheduleTexting.gs),
   and click **Save**.
3. Back in the sheet, reload the page. A **Schedule** menu appears. It only
   shows for editors, so employees never see it.
4. Choose **Schedule > Set up Twilio...** and enter your Account SID,
   Auth Token and Twilio number. The first time, Google asks you to
   authorize the script (it needs to read the sheets and contact Twilio).
5. Choose **Schedule > Send myself a test text**.

If the phones spreadsheet is ever recreated, update `PHONES_SPREADSHEET_ID`
at the top of the script with the ID from its URL.

## Using it

- **Text everyone this month's schedule**: run it from the month's tab.
  Each employee with a phone number gets their shifts, hours and a link to
  the sheet.
- **Text selected employee(s) only**: click a cell in the rows you want
  (Ctrl/Cmd-click for more than one), then run it.
- **Create next month's tab**: run it from the latest month's tab. It
  adds the next month with the same employees, dropdowns, colors and
  totals, and leaves the shifts blank.

To add an employee, insert a row anywhere between the first and last
employee so the totals formulas grow to include it, then add the same
name to the phones spreadsheet.
