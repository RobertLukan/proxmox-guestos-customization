/**
 * Multi-step wizard shell. Expects:
 *   form[data-wizard]
 *   .wizard-steps li[data-step]
 *   .wizard-panel[data-step][hidden]
 *   #wizard-back, #wizard-next, #wizard-submit
 *
 * Panels/steps with ``data-wizard-disabled="true"`` are skipped (e.g. Disks on Win11).
 */
(function (global) {
  'use strict';

  function initWizard(form, options) {
    options = options || {};
    var allPanels = Array.prototype.slice.call(form.querySelectorAll('.wizard-panel'));
    var allStepItems = Array.prototype.slice.call(form.querySelectorAll('.wizard-steps [data-step]'));
    var backBtn = form.querySelector('#wizard-back');
    var nextBtn = form.querySelector('#wizard-next');
    var submitBtn = form.querySelector('#wizard-submit');
    var current = 0;
    var panels = [];
    var stepItems = [];

    function isDisabled(el) {
      return el && el.getAttribute('data-wizard-disabled') === 'true';
    }

    function refreshPanelList() {
      panels = allPanels.filter(function (p) { return !isDisabled(p); });
      stepItems = allStepItems.filter(function (li) { return !isDisabled(li); });
      allPanels.forEach(function (p) {
        if (isDisabled(p)) {
          p.setAttribute('hidden', '');
          p.style.display = 'none';
        } else {
          p.style.display = '';
        }
      });
      allStepItems.forEach(function (li) {
        if (isDisabled(li)) {
          li.style.display = 'none';
          li.setAttribute('aria-hidden', 'true');
        } else {
          li.style.display = '';
          li.removeAttribute('aria-hidden');
        }
      });
      if (current >= panels.length) current = Math.max(0, panels.length - 1);
    }

    function show(index) {
      refreshPanelList();
      if (!panels.length) return;
      current = Math.max(0, Math.min(index, panels.length - 1));
      allPanels.forEach(function (p) {
        if (isDisabled(p) || p !== panels[current]) p.setAttribute('hidden', '');
        else p.removeAttribute('hidden');
      });
      allStepItems.forEach(function (li) {
        li.classList.remove('is-active', 'is-done');
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
      nextBtn.disabled = false;
    }

    function goNext() {
      refreshPanelList();
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
      refreshPanelList();
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
      refreshPanelList();
      for (var i = 0; i < panels.length; i++) {
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

    refreshPanelList();
    show(0);

    return {
      show: show,
      goNext: goNext,
      goBack: goBack,
      getCurrent: function () { return current; },
      panels: panels,
      refresh: function () { show(current); },
    };
  }

  global.GuestOSWizard = { init: initWizard };
})(window);
