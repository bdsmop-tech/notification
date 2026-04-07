(function () {
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();

  const titleEl = document.getElementById("screenTitle");
  const tzLine = document.getElementById("tzLine");
  const mainRoot = document.getElementById("main");
  const mainSheet = document.getElementById("mainSheet");
  const globalErr = document.getElementById("globalErr");
  const tabs = document.getElementById("tabs");

  /** Вкладки нижней панели — для свайпа и анимации перелистывания */
  const TAB_ORDER = ["active", "today", "history", "new", "settings"];

  let me = null;

  const state = {
    view: "active",
    backFromDetail: "active",
    activePage: 0,
    historyPage: 0,
    detailId: null,
    editMode: null,
    calYear: new Date().getFullYear(),
    calMonth: new Date().getMonth() + 1,
    newDraft: {
      from_history_id: null,
      text: "",
      date: "",
      time: "",
      spam: "once",
      customSpam: 60,
    },
  };

  function theme() {
    /* Палитра задаётся в style.css; не заливаем body цветами Telegram. */
    document.body.style.backgroundColor = "";
    document.body.style.color = "";
  }
  if (tg.onEvent) tg.onEvent("themeChanged", theme);
  theme();

  function showErr(msg) {
    globalErr.hidden = !msg;
    globalErr.textContent = msg || "";
  }

  function authHeaders(json) {
    const d = tg.initData;
    if (!d) return null;
    const h = { Authorization: "tma " + d };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  function errDetail(body) {
    if (!body || typeof body !== "object") return null;
    const d = body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d[0] && d[0].msg) return d[0].msg;
    return JSON.stringify(d);
  }

  async function api(path, options) {
    const o = options != null && typeof options === "object" ? options : {};
    const method = typeof o.method === "string" ? o.method : "GET";
    const body = Object.prototype.hasOwnProperty.call(o, "body") ? o.body : undefined;
    const jsonBody = typeof body === "string";
    const base = authHeaders(jsonBody);
    if (!base) throw new Error("Откройте из Telegram.");
    let extra = {};
    if (o.headers != null && typeof o.headers === "object" && !Array.isArray(o.headers)) {
      try {
        extra = Object.assign({}, o.headers);
      } catch (_) {
        extra = {};
      }
    }
    const headers = Object.assign({}, base, extra);
    const init = { method: method, headers: headers };
    if (body !== undefined && method !== "GET" && method !== "HEAD") {
      init.body = body;
    }
    const r = await fetch(path, init);
    const text = await r.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch (_) {
      payload = { raw: text };
    }
    if (!r.ok) {
      throw new Error(errDetail(payload) || "Ошибка " + r.status);
    }
    return payload;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function clearMain() {
    if (mainSheet) mainSheet.innerHTML = "";
  }

  function setTitle(t) {
    titleEl.textContent = t;
  }

  function tabActive() {
    tabs.querySelectorAll(".tab").forEach(function (b) {
      b.classList.toggle("tab--on", b.getAttribute("data-view") === state.view);
    });
    const on = tabs.querySelector(".tab--on");
    if (
      on &&
      on.classList.contains("tab--item") &&
      typeof on.scrollIntoView === "function"
    ) {
      on.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
    }
  }

  async function loadMe() {
    try {
      me = await api("/api/me");
      tzLine.textContent = me.tz_label ? "Пояс: " + me.tz_label : "";
    } catch (e) {
      tzLine.textContent = "";
    }
  }

  function openDetail(id, back) {
    state.detailId = id;
    state.backFromDetail = back || state.view;
    state.editMode = null;
    state.view = "detail";
    tabActive();
    render();
  }

  function backFromDetail() {
    state.detailId = null;
    state.view = state.backFromDetail;
    state.editMode = null;
    tabActive();
    render();
  }

  function fmtSpam(r) {
    if (r.spam_until_read) return ", до «Прочитал»";
    if (r.spam_interval_seconds) return ", каждые " + r.spam_interval_seconds + " с";
    return ", один раз";
  }

  function rowReminder(r, onPick) {
    const li = el("button", "card");
    const t = el("span", "card__time", r.fire_at_local + fmtSpam(r));
    const tx = el("span", "card__text", r.text);
    li.appendChild(t);
    li.appendChild(tx);
    li.type = "button";
    li.addEventListener("click", function () {
      onPick(r);
    });
    return li;
  }

  async function renderActive() {
    setTitle("Активные");
    showErr("");
    const box = el("div", "stack");
    const status = el("p", "hint", "Загрузка…");
    box.appendChild(status);
    mainSheet.appendChild(box);
    try {
      const data = await api("/api/reminders/active?page=" + state.activePage);
      box.innerHTML = "";
      if (!data.reminders.length) {
        box.appendChild(el("p", "hint", "Нет активных напоминаний."));
      } else {
        data.reminders.forEach(function (r) {
          box.appendChild(
            rowReminder(r, function () {
              openDetail(r.id, "active");
            }),
          );
        });
      }
      const nav = el("div", "row");
      if (data.page > 0) {
        const b = el("button", "btn btn--ghost", "← Пред.");
        b.addEventListener("click", function () {
          state.activePage = data.page - 1;
          render();
        });
        nav.appendChild(b);
      }
      if (data.page < data.pages - 1) {
        const b = el("button", "btn btn--ghost", "След. →");
        b.addEventListener("click", function () {
          state.activePage = data.page + 1;
          render();
        });
        nav.appendChild(b);
      }
      if (nav.children.length) box.appendChild(nav);
    } catch (e) {
      status.textContent = String(e.message || e);
    }
  }

  async function renderToday() {
    setTitle("Сегодня");
    showErr("");
    const box = el("div", "stack");
    mainSheet.appendChild(box);
    try {
      const data = await api("/api/reminders/today");
      if (!data.reminders.length) {
        box.appendChild(el("p", "hint", "На сегодня ничего нет."));
      } else {
        data.reminders.forEach(function (r) {
          box.appendChild(
            rowReminder(r, function () {
              openDetail(r.id, "today");
            }),
          );
        });
      }
    } catch (e) {
      box.appendChild(el("p", "err", String(e.message || e)));
    }
  }

  async function renderHistory() {
    setTitle("История");
    showErr("");
    const box = el("div", "stack");
    mainSheet.appendChild(box);
    try {
      const data = await api("/api/reminders/history?page=" + state.historyPage);
      if (!data.reminders.length) {
        box.appendChild(el("p", "hint", "История пуста."));
      } else {
        data.reminders.forEach(function (r) {
          const li = el("button", "card");
          const sub = r.closed_at_local ? " → " + r.closed_at_local : "";
          const t = el("span", "card__time", r.fire_at_local + sub);
          const tx = el("span", "card__text", r.text);
          li.appendChild(t);
          li.appendChild(tx);
          li.type = "button";
          li.addEventListener("click", function () {
            state.newDraft.from_history_id = r.id;
            state.newDraft.text = r.text;
            state.newDraft.date = "";
            state.newDraft.time = "";
            state.newDraft.spam = "once";
            state.view = "new";
            tabActive();
            render({
              tabDir: TAB_ORDER.indexOf("new") - TAB_ORDER.indexOf("history"),
            });
          });
          box.appendChild(li);
        });
      }
      const nav = el("div", "row");
      if (data.page > 0) {
        const b = el("button", "btn btn--ghost", "← Пред.");
        b.addEventListener("click", function () {
          state.historyPage = data.page - 1;
          render();
        });
        nav.appendChild(b);
      }
      if (data.page < data.pages - 1) {
        const b = el("button", "btn btn--ghost", "След. →");
        b.addEventListener("click", function () {
          state.historyPage = data.page + 1;
          render();
        });
        nav.appendChild(b);
      }
      if (nav.children.length) box.appendChild(nav);
    } catch (e) {
      box.appendChild(el("p", "err", String(e.message || e)));
    }
  }

  async function renderNew() {
    setTitle(state.newDraft.from_history_id ? "Повтор" : "Создать напоминание");
    showErr("");
    clearMain();
    const f = el("div", "form");

    if (state.newDraft.from_history_id) {
      f.appendChild(el("p", "hint", "Тот же текст — выбери дату и время."));
      const prev = el("div", "preview", state.newDraft.text);
      f.appendChild(prev);
    } else {
      const lab = el("label", "label", "Текст");
      const ta = document.createElement("textarea");
      ta.className = "input input--area";
      ta.rows = 3;
      ta.value = state.newDraft.text;
      ta.addEventListener("input", function () {
        state.newDraft.text = ta.value;
      });
      f.appendChild(lab);
      f.appendChild(ta);
    }

    {
      const calBox = el("div", "cal");
      const calHead = el("div", "row cal__head");
      const prev = el("button", "btn btn--ghost", "«");
      const next = el("button", "btn btn--ghost", "»");
      const cap = el("span", "cal__cap", "");
      calHead.appendChild(prev);
      calHead.appendChild(cap);
      calHead.appendChild(next);
      calBox.appendChild(calHead);
      const grid = el("div", "cal__grid");
      calBox.appendChild(grid);

      async function paintCal() {
        const c = await api("/api/calendar/" + state.calYear + "/" + state.calMonth);
        cap.textContent = c.month_label;
        grid.innerHTML = "";
        c.weekday_names.forEach(function (n) {
          grid.appendChild(el("div", "cal__wd", n));
        });
        c.weeks.forEach(function (week) {
          week.forEach(function (d) {
            const cell = el("button", "cal__day");
            if (d == null) {
              cell.classList.add("cal__day--muted");
              cell.textContent = "";
              cell.disabled = true;
            } else {
              cell.textContent = String(d);
              cell.type = "button";
              const y = state.calYear;
              const m = state.calMonth;
              cell.addEventListener("click", function () {
                const mm = String(m).padStart(2, "0");
                const dd = String(d).padStart(2, "0");
                state.newDraft.date = y + "-" + mm + "-" + dd;
                Array.from(grid.querySelectorAll(".cal__day--pick")).forEach(function (x) {
                  x.classList.remove("cal__day--pick");
                });
                cell.classList.add("cal__day--pick");
              });
            }
            grid.appendChild(cell);
          });
        });
      }

      prev.addEventListener("click", function () {
        state.calMonth -= 1;
        if (state.calMonth < 1) {
          state.calMonth = 12;
          state.calYear -= 1;
        }
        paintCal();
      });
      next.addEventListener("click", function () {
        state.calMonth += 1;
        if (state.calMonth > 12) {
          state.calMonth = 1;
          state.calYear += 1;
        }
        paintCal();
      });
      f.appendChild(el("label", "label", "Дата"));
      f.appendChild(calBox);
      paintCal().catch(function (e) {
        showErr(String(e.message || e));
      });

      const tLab = el("label", "label", "Время (16:43 или 16 43)");
      const tIn = el("input", "input");
      tIn.value = state.newDraft.time;
      tIn.addEventListener("input", function () {
        state.newDraft.time = tIn.value;
      });
      f.appendChild(tLab);
      f.appendChild(tIn);

      const chips = el("div", "chips");
      ["09:00", "12:00", "15:00", "18:00", "21:00"].forEach(function (s) {
        const b = el("button", "chip", s);
        b.type = "button";
        b.addEventListener("click", function () {
          tIn.value = s.replace(":", " ");
          state.newDraft.time = tIn.value;
        });
        chips.appendChild(b);
      });
      f.appendChild(chips);
    }

    f.appendChild(el("label", "label", "Повтор"));
    const spamSel = document.createElement("select");
    spamSel.className = "input input--select";
    spamSel.setAttribute("aria-label", "Режим повтора");
    const spamOpts = [
      ["once", "Один раз"],
      ["until_read", "До «Прочитал»"],
      ["i30", "Каждые 30 с"],
      ["i60", "Каждые 60 с"],
      ["i120", "Каждые 120 с"],
      ["custom", "Свой интервал (сек)…"],
    ];
    spamOpts.forEach(function (x) {
      const o = document.createElement("option");
      o.value = x[0];
      o.textContent = x[1];
      if (state.newDraft.spam === x[0]) o.selected = true;
      spamSel.appendChild(o);
    });
    const custWrap = el("div", "spam-custom");
    const cust = el("input", "input");
    cust.type = "number";
    cust.min = "0";
    cust.value = String(state.newDraft.customSpam);
    cust.addEventListener("input", function () {
      state.newDraft.customSpam = parseInt(cust.value, 10) || 0;
    });
    custWrap.appendChild(
      el(
        "small",
        "hint",
        "Секунды (мин. " + (me && me.min_spam_interval_seconds ? me.min_spam_interval_seconds : 15) + ")",
      ),
    );
    custWrap.appendChild(cust);
    function syncSpamCustom() {
      const on = spamSel.value === "custom";
      custWrap.hidden = !on;
      cust.disabled = !on;
    }
    spamSel.addEventListener("change", function () {
      state.newDraft.spam = spamSel.value;
      syncSpamCustom();
    });
    syncSpamCustom();
    f.appendChild(spamSel);
    f.appendChild(custWrap);

    const submit = el("button", "btn", "Создать");
    submit.type = "button";
    submit.addEventListener("click", async function () {
      showErr("");
      try {
        const body = {
          spam_variant: state.newDraft.spam,
          spam_interval_seconds: state.newDraft.customSpam,
        };
        if (state.newDraft.from_history_id) {
          body.from_history_id = state.newDraft.from_history_id;
        } else {
          body.text = state.newDraft.text.trim();
          body.date = state.newDraft.date;
          body.time = state.newDraft.time.trim();
        }
        await api("/api/reminders", { method: "POST", body: JSON.stringify(body) });
        state.newDraft = {
          from_history_id: null,
          text: "",
          date: "",
          time: "",
          spam: "once",
          customSpam: 60,
        };
        state.view = "active";
        state.activePage = 0;
        tabActive();
        render();
      } catch (e) {
        showErr(String(e.message || e));
      }
    });
    f.appendChild(submit);

    if (state.newDraft.from_history_id) {
      const cancel = el("button", "btn btn--ghost", "Отмена");
      cancel.type = "button";
      cancel.addEventListener("click", function () {
        state.newDraft.from_history_id = null;
        state.view = "history";
        tabActive();
        render();
      });
      f.appendChild(cancel);
    }

    mainSheet.appendChild(f);
  }

  async function renderDetail() {
    setTitle("Напоминание");
    showErr("");
    clearMain();
    const box = el("div", "stack");
    mainSheet.appendChild(box);
    try {
      const r = await api("/api/reminders/" + state.detailId);
      box.appendChild(el("p", "detail__time", r.fire_at_local + fmtSpam(r)));
      box.appendChild(el("p", "detail__text", r.text));

      const row = el("div", "row row--wrap");
      function btn(label, fn) {
        const b = el("button", "btn btn--small", label);
        b.type = "button";
        b.addEventListener("click", fn);
        return b;
      }
      row.appendChild(
        btn("Текст", function () {
          state.editMode = "text";
          render();
        }),
      );
      row.appendChild(
        btn("Дата/время", function () {
          state.editMode = "datetime";
          const p = r.date_local.split("-");
          state.calYear = parseInt(p[0], 10);
          state.calMonth = parseInt(p[1], 10);
          render();
        }),
      );
      row.appendChild(
        btn("Повтор", function () {
          state.editMode = "spam";
          render();
        }),
      );
      box.appendChild(row);

      const row2 = el("div", "row row--wrap");
      row2.appendChild(
        btn("Стоп", async function () {
          try {
            await api("/api/reminders/" + state.detailId + "/stop", { method: "POST", body: "{}" });
            backFromDetail();
            render();
          } catch (e) {
            showErr(String(e.message || e));
          }
        }),
      );
      row2.appendChild(
        btn("В архив", async function () {
          try {
            await api("/api/reminders/" + state.detailId + "/archive", { method: "POST", body: "{}" });
            backFromDetail();
            render();
          } catch (e) {
            showErr(String(e.message || e));
          }
        }),
      );
      box.appendChild(row2);

      if (state.editMode === "text") {
        const panel = el("div", "panel");
        panel.appendChild(el("p", "label", "Новый текст"));
        const ta = document.createElement("textarea");
        ta.className = "input input--area";
        ta.value = r.text;
        panel.appendChild(ta);
        const ok = el("button", "btn", "Сохранить");
        ok.type = "button";
        ok.addEventListener("click", async function () {
          try {
            await api("/api/reminders/" + state.detailId, {
              method: "PATCH",
              body: JSON.stringify({ text: ta.value }),
            });
            state.editMode = null;
            render();
          } catch (e) {
            showErr(String(e.message || e));
          }
        });
        panel.appendChild(ok);
        const cx = el("button", "btn btn--ghost", "Отмена");
        cx.type = "button";
        cx.addEventListener("click", function () {
          state.editMode = null;
          render();
        });
        panel.appendChild(cx);
        box.appendChild(panel);
      }

      if (state.editMode === "datetime") {
        const panel = el("div", "panel");
        panel.appendChild(el("p", "label", "Новая дата и время"));
        const calBox = el("div", "cal");
        const calHead = el("div", "row cal__head");
        const prev = el("button", "btn btn--ghost", "«");
        const next = el("button", "btn btn--ghost", "»");
        const cap = el("span", "cal__cap", "");
        calHead.appendChild(prev);
        calHead.appendChild(cap);
        calHead.appendChild(next);
        calBox.appendChild(calHead);
        const grid = el("div", "cal__grid");
        calBox.appendChild(grid);
        let pickDate = r.date_local;
        const tIn = el("input", "input");
        tIn.value = r.time_local;

        async function paintCal() {
          const c = await api("/api/calendar/" + state.calYear + "/" + state.calMonth);
          cap.textContent = c.month_label;
          grid.innerHTML = "";
          c.weekday_names.forEach(function (n) {
            grid.appendChild(el("div", "cal__wd", n));
          });
          c.weeks.forEach(function (week) {
            week.forEach(function (d) {
              const cell = el("button", "cal__day");
              if (d == null) {
                cell.classList.add("cal__day--muted");
                cell.disabled = true;
              } else {
                cell.textContent = String(d);
                cell.type = "button";
                const y = state.calYear;
                const m = state.calMonth;
                cell.addEventListener("click", function () {
                  const mm = String(m).padStart(2, "0");
                  const dd = String(d).padStart(2, "0");
                  pickDate = y + "-" + mm + "-" + dd;
                  Array.from(grid.querySelectorAll(".cal__day--pick")).forEach(function (x) {
                    x.classList.remove("cal__day--pick");
                  });
                  cell.classList.add("cal__day--pick");
                });
              }
              grid.appendChild(cell);
            });
          });
        }
        prev.addEventListener("click", function () {
          state.calMonth -= 1;
          if (state.calMonth < 1) {
            state.calMonth = 12;
            state.calYear -= 1;
          }
          paintCal();
        });
        next.addEventListener("click", function () {
          state.calMonth += 1;
          if (state.calMonth > 12) {
            state.calMonth = 1;
            state.calYear += 1;
          }
          paintCal();
        });
        panel.appendChild(calBox);
        await paintCal();
        panel.appendChild(tIn);
        const ok = el("button", "btn", "Сохранить");
        ok.type = "button";
        ok.addEventListener("click", async function () {
          try {
            await api("/api/reminders/" + state.detailId, {
              method: "PATCH",
              body: JSON.stringify({ date: pickDate, time: tIn.value.trim() }),
            });
            state.editMode = null;
            render();
          } catch (e) {
            showErr(String(e.message || e));
          }
        });
        panel.appendChild(ok);
        const cx = el("button", "btn btn--ghost", "Отмена");
        cx.type = "button";
        cx.addEventListener("click", function () {
          state.editMode = null;
          render();
        });
        panel.appendChild(cx);
        box.appendChild(panel);
      }

      if (state.editMode === "spam") {
        const panel = el("div", "panel");
        panel.appendChild(el("label", "label", "Режим повтора"));
        let pick = r.spam_variant === "custom" ? "custom" : r.spam_variant;
        const spamSel = document.createElement("select");
        spamSel.className = "input input--select";
        const edOpts = [
          ["once", "Один раз"],
          ["until_read", "До «Прочитал»"],
          ["i30", "Каждые 30 с"],
          ["i60", "Каждые 60 с"],
          ["i120", "Каждые 120 с"],
          ["custom", "Свой интервал (сек)…"],
        ];
        edOpts.forEach(function (x) {
          const o = document.createElement("option");
          o.value = x[0];
          o.textContent = x[1];
          if (pick === x[0]) o.selected = true;
          spamSel.appendChild(o);
        });
        const ci = el("input", "input");
        ci.type = "number";
        ci.min = "0";
        ci.value = String(r.spam_interval_seconds || 60);
        const custWrap = el("div", "spam-custom");
        custWrap.appendChild(el("small", "hint", "Секунды для своего интервала"));
        custWrap.appendChild(ci);
        function syncEd() {
          pick = spamSel.value;
          const on = pick === "custom";
          custWrap.hidden = !on;
          ci.disabled = !on;
        }
        spamSel.addEventListener("change", syncEd);
        syncEd();
        panel.appendChild(spamSel);
        panel.appendChild(custWrap);
        const ok = el("button", "btn", "Сохранить");
        ok.type = "button";
        ok.addEventListener("click", async function () {
          try {
            const pv = spamSel.value;
            await api("/api/reminders/" + state.detailId + "/spam", {
              method: "PATCH",
              body: JSON.stringify({
                spam_variant: pv,
                spam_interval_seconds: pv === "custom" ? parseInt(ci.value, 10) || 0 : 0,
              }),
            });
            state.editMode = null;
            render();
          } catch (e) {
            showErr(String(e.message || e));
          }
        });
        panel.appendChild(ok);
        const cx = el("button", "btn btn--ghost", "Отмена");
        cx.type = "button";
        cx.addEventListener("click", function () {
          state.editMode = null;
          render();
        });
        panel.appendChild(cx);
        box.appendChild(panel);
      }

      const back = el("button", "btn btn--ghost", "← Назад");
      back.type = "button";
      back.addEventListener("click", backFromDetail);
      box.appendChild(back);
    } catch (e) {
      box.appendChild(el("p", "err", String(e.message || e)));
      const back = el("button", "btn btn--ghost", "← Назад");
      back.type = "button";
      back.addEventListener("click", backFromDetail);
      box.appendChild(back);
    }
  }

  async function renderSettings() {
    setTitle("Настройки");
    showErr("");
    clearMain();
    const box = el("div", "stack");
    mainSheet.appendChild(box);
    try {
      me = await api("/api/me");
      tzLine.textContent = me.tz_label ? "Пояс: " + me.tz_label : "";

      box.appendChild(el("p", "label", "Часовой пояс (смещение от UTC)"));
      const grid = el("div", "tzgrid");
      for (let h = -12; h <= 14; h++) {
        const b = el("button", "tz", h === 0 ? "0" : (h > 0 ? "+" + h : String(h)));
        b.type = "button";
        if (me.offset_hours === h) b.classList.add("tz--on");
        b.addEventListener("click", async function () {
          try {
            await api("/api/me/timezone", {
              method: "POST",
              body: JSON.stringify({ offset_hours: h }),
            });
            await loadMe();
            render();
          } catch (e) {
            showErr(String(e.message || e));
          }
        });
        grid.appendChild(b);
      }
      box.appendChild(grid);

      const qh = el("button", "btn", me.quiet_hours_enabled ? "Тихие часы: вкл" : "Тихие часы: выкл");
      qh.type = "button";
      qh.addEventListener("click", async function () {
        try {
          const r = await api("/api/me/quiet-hours/toggle", { method: "POST", body: "{}" });
          me.quiet_hours_enabled = r.quiet_hours_enabled;
          qh.textContent = r.quiet_hours_enabled ? "Тихие часы: вкл" : "Тихие часы: выкл";
        } catch (e) {
          showErr(String(e.message || e));
        }
      });
      box.appendChild(qh);
      box.appendChild(
        el(
          "p",
          "hint",
          "Тихие часы 23:00–07:00 по вашему поясу — напоминания переносятся на утро.",
        ),
      );

      const help = el("button", "btn btn--ghost", "Справка");
      help.type = "button";
      help.addEventListener("click", function () {
        state.view = "help";
        render();
      });
      box.appendChild(help);
    } catch (e) {
      box.appendChild(el("p", "err", String(e.message || e)));
    }
  }

  function renderHelp() {
    setTitle("Справка");
    clearMain();
    const box = el("div", "stack");
    box.appendChild(
      el(
        "pre",
        "help",
        [
          "• Создать — текст, дата в календаре, время, режим повтора.",
          "• История — нажатие: повтор с тем же текстом.",
          "• В уведомлении в чате: Прочитал, Стоп, отложить — как в боте.",
          "• Пояс UTC: кнопки −12…+14.",
        ].join("\n"),
      ),
    );
    const back = el("button", "btn btn--ghost", "← Назад");
    back.type = "button";
    back.addEventListener("click", function () {
      state.view = "settings";
      tabActive();
      render();
    });
    mainSheet.appendChild(box);
    mainSheet.appendChild(back);
  }

  function renderContent() {
    if (!tg.initData) {
      mainSheet.appendChild(el("p", "err", "Откройте мини-приложение из Telegram."));
      return;
    }
    if (state.view === "active") renderActive();
    else if (state.view === "today") renderToday();
    else if (state.view === "history") renderHistory();
    else if (state.view === "new") renderNew();
    else if (state.view === "settings") renderSettings();
    else if (state.view === "help") renderHelp();
    else if (state.view === "detail") renderDetail();
  }

  function render(opts) {
    opts = opts || {};
    if (!mainSheet) return;
    showErr("");
    tabActive();
    const dir = opts.tabDir;
    const useAnim =
      typeof dir === "number" &&
      dir !== 0 &&
      TAB_ORDER.indexOf(state.view) >= 0 &&
      mainSheet.children.length > 0;

    function paint() {
      clearMain();
      renderContent();
    }

    if (useAnim) {
      const exit = dir > 0 ? "main__sheet--exit-left" : "main__sheet--exit-right";
      const enter = dir > 0 ? "main__sheet--enter-from-right" : "main__sheet--enter-from-left";
      mainSheet.classList.add(exit);
      window.setTimeout(function () {
        paint();
        mainSheet.classList.remove("main__sheet--exit-left", "main__sheet--exit-right");
        mainSheet.classList.add(enter);
        window.setTimeout(function () {
          mainSheet.classList.remove("main__sheet--enter-from-right", "main__sheet--enter-from-left");
        }, 320);
      }, 200);
      return;
    }
    paint();
  }

  tabs.addEventListener("click", function (ev) {
    const t = ev.target.closest(".tab");
    if (!t) return;
    const v = t.getAttribute("data-view");
    if (!v) return;
    const prev = state.view;
    const iPrev = TAB_ORDER.indexOf(prev);
    const iNext = TAB_ORDER.indexOf(v);
    state.view = v;
    state.detailId = null;
    state.editMode = null;
    if (v === "active") state.activePage = 0;
    if (v === "history") state.historyPage = 0;
    const tabDir = iPrev >= 0 && iNext >= 0 ? iNext - iPrev : 0;
    render({ tabDir: tabDir });
  });

  if (mainRoot) {
    let touchX = 0;
    mainRoot.addEventListener(
      "touchstart",
      function (e) {
        touchX = e.changedTouches[0].clientX;
      },
      { passive: true },
    );
    mainRoot.addEventListener(
      "touchend",
      function (e) {
        if (state.view === "detail" || state.view === "help") return;
        const idx = TAB_ORDER.indexOf(state.view);
        if (idx < 0) return;
        const dx = e.changedTouches[0].clientX - touchX;
        if (Math.abs(dx) < 50) return;
        if (dx < 0 && idx < TAB_ORDER.length - 1) {
          state.view = TAB_ORDER[idx + 1];
          state.detailId = null;
          state.editMode = null;
          if (state.view === "active") state.activePage = 0;
          if (state.view === "history") state.historyPage = 0;
          render({ tabDir: 1 });
        } else if (dx > 0 && idx > 0) {
          state.view = TAB_ORDER[idx - 1];
          state.detailId = null;
          state.editMode = null;
          if (state.view === "active") state.activePage = 0;
          if (state.view === "history") state.historyPage = 0;
          render({ tabDir: -1 });
        }
      },
      { passive: true },
    );
  }

  loadMe().then(render);
})();
