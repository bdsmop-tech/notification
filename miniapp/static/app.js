(function () {
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();

  const statusEl = document.getElementById("status");
  const listEl = document.getElementById("list");
  const tzEl = document.getElementById("tz");

  function applyTheme() {
    const p = tg.themeParams;
    document.body.style.backgroundColor = p.bg_color || "#212121";
    document.body.style.color = p.text_color || "#f5f5f5";
  }

  if (tg.onEvent) {
    tg.onEvent("themeChanged", applyTheme);
  }
  applyTheme();

  async function loadMe() {
    const auth = tg.initData;
    if (!auth) return;
    try {
      const r = await fetch("/api/me", {
        headers: { Authorization: "tma " + auth },
      });
      if (!r.ok) return;
      const data = await r.json();
      if (data.tz_label) {
        tzEl.textContent = "Пояс: " + data.tz_label;
      }
    } catch (_) {
      /* ignore */
    }
  }

  async function loadReminders() {
    statusEl.textContent = "Загрузка…";
    listEl.innerHTML = "";
    const auth = tg.initData;
    if (!auth) {
      statusEl.textContent = "Откройте мини-приложение из Telegram (кнопка в боте).";
      return;
    }
    try {
      const r = await fetch("/api/reminders/active", {
        headers: { Authorization: "tma " + auth },
      });
      if (r.status === 401) {
        statusEl.textContent = "Сессия устарела — закройте и откройте снова.";
        return;
      }
      if (!r.ok) {
        statusEl.textContent = "Ошибка " + r.status;
        return;
      }
      const data = await r.json();
      const items = data.reminders || [];
      statusEl.textContent = items.length ? "" : "Нет активных напоминаний.";
      for (const it of items) {
        const li = document.createElement("li");
        const t = document.createElement("span");
        t.className = "time";
        t.textContent = it.fire_at_local;
        li.appendChild(t);
        li.appendChild(document.createTextNode(it.text));
        listEl.appendChild(li);
      }
    } catch (e) {
      statusEl.textContent = String(e.message || e);
    }
  }

  document.getElementById("reload").addEventListener("click", function () {
    loadMe();
    loadReminders();
  });

  loadMe();
  loadReminders();
})();
