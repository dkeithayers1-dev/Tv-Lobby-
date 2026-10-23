const token = document.querySelector('meta[name="csrf"]')?.content || "";

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const open = [...document.querySelectorAll("dialog[open]")];
  if (!open.length) return;
  event.preventDefault();
  open[open.length - 1].close();
});

document.querySelectorAll("dialog.auto").forEach((dialog) => {
  if (!dialog.open) dialog.showModal();
});

document.querySelectorAll("[data-drag]").forEach((card) => {
  card.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", card.dataset.id);
    event.dataTransfer.setData("text/stage", card.dataset.stage);
  });
});

document.querySelectorAll("[data-drop]").forEach((column) => {
  column.addEventListener("dragover", (event) => event.preventDefault());
  column.addEventListener("drop", async (event) => {
    event.preventDefault();
    const contactId = event.dataTransfer.getData("text/plain");
    const from = Number(event.dataTransfer.getData("text/stage"));
    const stage = Number(column.dataset.drop);
    let confirmBackward = false;
    if (stage < from) {
      confirmBackward = window.confirm("Move this prospect backward? Milestone dates stay.");
      if (!confirmBackward) return;
    }
    const response = await fetch("/pipeline/move", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token, "Accept": "application/json" },
      body: JSON.stringify({ contact_id: contactId, stage, confirm_backward: confirmBackward }),
    });
    if (response.ok) window.location.reload();
  });
});
