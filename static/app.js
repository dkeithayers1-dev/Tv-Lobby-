const fileInput = document.getElementById('file-input');
const entriesEl = document.getElementById('entries');
const statusEl = document.getElementById('status');
const submitBtn = document.getElementById('submit-btn');

const receipts = new Map(); // id -> parsed data

fileInput.addEventListener('change', async () => {
  const files = Array.from(fileInput.files);
  fileInput.value = '';
  for (const file of files) {
    await uploadAndParse(file);
  }
});

async function uploadAndParse(file) {
  statusEl.textContent = `Reading ${file.name}...`;
  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/parse', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = `${file.name}: ${data.error}`;
      return;
    }
    receipts.set(data.id, data);
    renderEntry(data);
    statusEl.textContent = '';
    submitBtn.style.display = 'block';
  } catch (err) {
    statusEl.textContent = `${file.name}: upload failed (${err.message})`;
  }
}

function renderEntry(data) {
  const card = document.createElement('div');
  card.className = 'receipt-card';
  card.dataset.id = data.id;
  card.innerHTML = `
    <button class="remove" type="button">Remove</button>
    <h3>${escapeHtml(data.vendor || 'Receipt')} — ${escapeHtml(data.original_filename)}</h3>

    <label>Date</label>
    <input type="date" class="f-date" value="${data.date || ''}">

    <label>Description</label>
    <input type="text" class="f-description" value="${escapeHtml(data.description || '')}">

    <label>Amount ($)</label>
    <input type="number" step="0.01" class="f-amount" value="${data.amount ?? ''}">

    <label>Mileage (optional)</label>
    <input type="number" step="0.01" class="f-mileage" value="${data.mileage ?? ''}">
  `;
  card.querySelector('.remove').addEventListener('click', () => {
    receipts.delete(data.id);
    card.remove();
    if (receipts.size === 0) submitBtn.style.display = 'none';
  });
  entriesEl.appendChild(card);
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}

submitBtn.addEventListener('click', async () => {
  const entries = [];
  document.querySelectorAll('.receipt-card').forEach(card => {
    const id = card.dataset.id;
    const original = receipts.get(id) || {};
    entries.push({
      id,
      date: card.querySelector('.f-date').value,
      description: card.querySelector('.f-description').value,
      amount: card.querySelector('.f-amount').value,
      mileage: card.querySelector('.f-mileage').value,
      from_addr: original.from_addr,
      to_addr: original.to_addr,
    });
  });

  if (entries.length === 0) return;

  submitBtn.disabled = true;
  statusEl.textContent = 'Saving to expense report...';

  try {
    const res = await fetch('/api/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries }),
    });
    const data = await res.json();

    if (data.errors && data.errors.length) {
      statusEl.textContent = `Some entries had problems: ${data.errors.join('; ')}`;
    } else if (data.emailed) {
      statusEl.textContent = `Added ${data.added_rows.length} expense(s) and emailed the updated report!`;
    } else {
      statusEl.textContent = `Added ${data.added_rows.length} expense(s), but email failed: ${data.email_error || 'unknown error'}`;
    }

    entriesEl.innerHTML = '';
    receipts.clear();
    submitBtn.style.display = 'none';
  } catch (err) {
    statusEl.textContent = `Failed to save: ${err.message}`;
  } finally {
    submitBtn.disabled = false;
  }
});
