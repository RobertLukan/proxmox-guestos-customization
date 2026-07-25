/**
 * Multi-step wizard shell. Expects:
 *   form[data-wizard]
 *   .wizard-steps li[data-step]
 *   .wizard-panel[data-step][hidden]
 *   #wizard-back, #wizard-next, #wizard-submit
 * Optional: form dispatches gos:before-next; return false via detail.cancel
 */
(function (global) {
  'use strict';

  function initWizard(form, options) {
    options = options || {};
    var panels = Array.prototype.slice.call(form.querySelectorAll('.wizard-panel'));
    var stepItems = Array.prototype.slice.call(form.querySelectorAll('.wizard-steps [data-step]'));
    var backBtn = form.querySelector('#wizard-back');
    var nextBtn = form.querySelector('#wizard-next');
    var submitBtn = form.querySelector('#wizard-submit');
    var current = 0;

    function show(index) {
      current = index;
      panels.forEach(function (p, i) {
        if (i === current) p.removeAttribute('hidden');
        else p.setAttribute('hidden', '');
      });
      stepItems.forEach(function (li, i) {
        li.classList.toggle('is-active', i === current);
        li.classList.toggle('is-done', i < current);
      });
      if (backBtn) backBtn.disabled = current === 0;
      if (nextBtn) nextBtn.hidden = current === panels.length - 1;
      if (submitBtn) submitBtn.hidden = current !== panels.length - 1;
      if (typeof options.onStep === 'function') options.onStep(current, panels[current]);
      refreshNextEnabled();
    }

    function refreshNextEnabled() {
      if (!nextBtn || nextBtn.hidden) return;
      // Soft gate: allow click; validateStep on click. Keep enabled for accessibility.
      nextBtn.disabled = false;
    }

    function goNext() {
      var panel = panels[current];
      if (window.GuestOSValidate && !GuestOSValidate.validateStep(panel)) {
        return false;
      }
      if (typeof options.beforeNext === 'function') {
        var blocked = options.beforeNext(current, panel) === false;
        if (blocked) return false;
      }
      if (current < panels.length - 1) show(current + 1);
      if (typeof options.onReview === 'function' && current === panels.length - 1) {
        options.onReview(panels[current]);
      }
      return true;
    }

    function goBack() {
      if (current > 0) show(current - 1);
    }

    if (backBtn) backBtn.addEventListener('click', function (e) {
      e.preventDefault();
      goBack();
    });
    if (nextBtn) nextBtn.addEventListener('click', function (e) {
      e.preventDefault();
      goNext();
      if (typeof options.onReview === 'function' && current === panels.length - 1) {
        options.onReview(panels[current]);
      }
    });

    form.addEventListener('submit', function (e) {
      // Validate all visible panels' required journey: re-validate every panel
      for (var i = 0; i < panels.length; i++) {
        // Temporarily show for validation of required fields in prior steps
        var wasHidden = panels[i].hasAttribute('hidden');
        panels[i].removeAttribute('hidden');
        var ok = !window.GuestOSValidate || GuestOSValidate.validateStep(panels[i]);
        if (wasHidden && i !== current) panels[i].setAttribute('hidden', '');
        if (!ok) {
          e.preventDefault();
          show(i);
          return;
        }
      }
      if (typeof options.onSubmit === 'function') {
        e.preventDefault();
        options.onSubmit(e);
      }
    });

    if (window.GuestOSValidate) {
      GuestOSValidate.bindLive(form);
    }

    show(0);

    return {
      show: show,
      goNext: goNext,
      goBack: goBack,
      getCurrent: function () { return current; },
      panels: panels,
    };
  }

  global.GuestOSWizard = { init: initWizard };
})(window);
