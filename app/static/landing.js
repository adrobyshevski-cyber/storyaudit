(function () {
  "use strict";

  var year = document.getElementById("year");
  if (year) year.textContent = new Date().getFullYear();

  var menu = document.getElementById("menu");
  var nav = document.getElementById("nav");
  if (menu && nav) {
    menu.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      document.body.classList.toggle("menu-open", open);
      menu.setAttribute("aria-expanded", String(open));
    });
    nav.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        nav.classList.remove("open");
        document.body.classList.remove("menu-open");
        menu.setAttribute("aria-expanded", "false");
      });
    });
  }

  var dialog = document.getElementById("sample-dialog");
  var frame = document.getElementById("embedded-report");
  var close = document.getElementById("close-report");
  if (dialog && frame) {
    document.querySelectorAll('[data-report="true"]').forEach(function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        if (!frame.getAttribute("src")) frame.src = frame.getAttribute("data-src");
        if (typeof dialog.showModal === "function") dialog.showModal();
        else { dialog.setAttribute("open", ""); dialog.scrollIntoView(); }
      });
    });
    if (close) close.addEventListener("click", function () {
      if (dialog.close) dialog.close(); else dialog.removeAttribute("open");
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog && dialog.close) dialog.close();
    });
  }

  var form = document.getElementById("audit-form");
  var feedback = document.getElementById("form-feedback");
  if (form) {
    form.addEventListener("submit", function (event) {
      var website = document.getElementById("site-url");
      var consent = form.querySelector('input[name="consent"]');
      var raw = (website && website.value || "").trim();
      if (!raw) {
        event.preventDefault();
        if (feedback) feedback.textContent = "Укажите адрес сайта.";
        if (website) website.focus();
        return;
      }
      var normalized = raw.indexOf("://") === -1 ? "https://" + raw : raw;
      var parsed;
      try { parsed = new URL(normalized); } catch (err) { parsed = null; }
      if (!parsed || ["http:", "https:"].indexOf(parsed.protocol) === -1 || parsed.hostname.indexOf(".") === -1) {
        event.preventDefault();
        if (feedback) feedback.textContent = "Введите корректный публичный адрес сайта.";
        if (website) website.focus();
        return;
      }
      if (consent && !consent.checked) {
        event.preventDefault();
        if (feedback) feedback.textContent = "Нужно согласие на обработку данных.";
        consent.focus();
        return;
      }
      if (website) website.value = normalized;
      var button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; button.textContent = "Создаём заявку…"; }
      if (feedback) feedback.textContent = "Создаём защищённую ссылку на страницу статуса…";
    });
  }

  var error = document.querySelector(".alert-public");
  if (error && form) {
    form.scrollIntoView({ behavior: "smooth", block: "center" });
  }
})();
