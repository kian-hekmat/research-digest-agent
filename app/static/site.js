// Papers Weekly: progressive enhancement for the public pages
// (docs/frontend-plan.md). Without JS every topic row shows and still works.
(function () {
  "use strict";

  // Keep each papers slider's <output> in sync.
  document.querySelectorAll(".papers input[type=range]").forEach(function (range) {
    var out = range.parentNode.querySelector("output");
    if (!out) return;
    range.addEventListener("input", function () { out.textContent = range.value; });
  });

  // Signup topic rows: show the first row and any filled or failed one, and
  // reveal more with "Add another topic".
  var list = document.querySelector("[data-topic-list]");
  if (!list) return;
  var cards = Array.prototype.slice.call(list.querySelectorAll(".topic-card"));
  var add = document.querySelector("[data-add-topic]");
  var count = document.querySelector("[data-topic-count]");
  var tools = document.querySelector("[data-topic-tools]");

  function visible() { return cards.filter(function (c) { return !c.hidden; }); }

  function update() {
    var shown = visible();
    if (count) count.textContent = shown.length + " of " + cards.length;
    if (add) add.hidden = shown.length >= cards.length;
    shown.forEach(function (c) {
      var rm = c.querySelector("[data-remove-topic]");
      if (rm) rm.hidden = shown.length <= 1;
    });
  }

  function reset(card) {
    var phrase = card.querySelector("input[name^=topic_]");
    var range = card.querySelector("input[type=range]");
    var out = card.querySelector("output");
    var err = card.querySelectorAll(".error");
    if (phrase) phrase.value = "";
    if (range) { range.value = range.defaultValue = "5"; if (out) out.textContent = "5"; }
    err.forEach(function (e) { e.remove(); });
  }

  cards.forEach(function (card, i) {
    var phrase = card.querySelector("input[name^=topic_]");
    var keep = i === 0 || (phrase && phrase.value.trim()) || card.querySelector(".error");
    card.hidden = !keep;
    var rm = card.querySelector("[data-remove-topic]");
    if (rm) rm.addEventListener("click", function () {
      reset(card);
      card.hidden = true;
      // Keep at least one row, and keep the visible rows first.
      list.appendChild(card);
      cards = Array.prototype.slice.call(list.querySelectorAll(".topic-card"));
      update();
    });
  });

  if (add) add.addEventListener("click", function () {
    var next = cards.filter(function (c) { return c.hidden; })[0];
    if (!next) return;
    next.hidden = false;
    var phrase = next.querySelector("input[name^=topic_]");
    if (phrase) phrase.focus();
    update();
  });

  if (tools) tools.hidden = false;
  update();
})();
