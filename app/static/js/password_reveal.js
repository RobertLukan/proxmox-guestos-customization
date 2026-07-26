/**
 * Brief password peek: eye button shows the value for a few seconds, then hides again.
 * Auto-enhances password inputs (opt out with data-no-reveal).
 */
(function (global) {
  'use strict';

  var SHOW_MS = 4000;

  var ICON_SHOW =
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M16 8s-3-5.5-8-5.5S0 8 0 8s3 5.5 8 5.5S16 8 16 8zM1.173 8a13.133 13.133 0 0 1 1.66-2.043C4.12 4.668 5.88 3.5 8 3.5c2.12 0 3.879 1.168 5.168 2.457A13.133 13.133 0 0 1 14.828 8c-.058.087-.122.183-.195.288-.335.48-.83 1.12-1.465 1.755C11.879 11.332 10.119 12.5 8 12.5c-2.12 0-3.879-1.168-5.168-2.457A13.134 13.134 0 0 1 1.172 8z"/>' +
    '<path d="M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM4.5 8a3.5 3.5 0 1 1 7 0 3.5 3.5 0 0 1-7 0z"/>' +
    '</svg>';

  var ICON_HIDE =
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M13.359 11.238C15.06 9.72 16 8 16 8s-3-5.5-8-5.5a7.028 7.028 0 0 0-2.79.588l.77.771A5.98 5.98 0 0 1 8 3.5c2.12 0 3.879 1.168 5.168 2.457A13.134 13.134 0 0 1 14.828 8c-.058.087-.122.183-.195.288-.335.48-.83 1.12-1.465 1.755-.165.165-.337.328-.517.486l.708.709z"/>' +
    '<path d="M11.297 9.176a3.5 3.5 0 0 0-4.474-4.474l.823.823a2.5 2.5 0 0 1 2.829 2.829l.822.822zm-2.945 1.35.822.822a3.5 3.5 0 0 1-4.474-4.474l.823.823a2.5 2.5 0 0 0 2.829 2.829z"/>' +
    '<path d="M3.35 5.47c-.18.16-.353.322-.518.487A13.134 13.134 0 0 0 1.172 8l.195.288c.335.48.83 1.12 1.465 1.755C4.121 11.332 5.881 12.5 8 12.5c.716 0 1.39-.133 2.02-.36l.77.772A7.029 7.029 0 0 1 8 13.5C3 13.5 0 8 0 8s.939-1.721 2.641-3.238l.708.709zm10.296 8.884-12-12 .708-.708 12 12-.708.708z"/>' +
    '</svg>';

  function clearTimer(input) {
    if (input._guestosRevealTimer) {
      global.clearTimeout(input._guestosRevealTimer);
      input._guestosRevealTimer = null;
    }
  }

  function setHidden(input, btn) {
    clearTimer(input);
    input.type = 'password';
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-label', 'Show password briefly');
    btn.title = 'Show password briefly';
    btn.innerHTML = ICON_SHOW;
  }

  function setVisible(input, btn) {
    clearTimer(input);
    input.type = 'text';
    btn.setAttribute('aria-pressed', 'true');
    btn.setAttribute('aria-label', 'Hide password');
    btn.title = 'Hide password';
    btn.innerHTML = ICON_HIDE;
    input._guestosRevealTimer = global.setTimeout(function () {
      setHidden(input, btn);
    }, SHOW_MS);
  }

  function enhance(input) {
    if (!input || input.dataset.noReveal === 'true' || input.dataset.revealReady === 'true') {
      return;
    }
    if (input.type !== 'password' && input.type !== 'text') {
      return;
    }

    var group = document.createElement('div');
    group.className = 'input-group password-reveal-group';

    var parent = input.parentNode;
    parent.insertBefore(group, input);
    group.appendChild(input);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-secondary password-reveal-btn';
    btn.tabIndex = 0;
    setHidden(input, btn);

    btn.addEventListener('click', function () {
      if (input.type === 'password') {
        setVisible(input, btn);
      } else {
        setHidden(input, btn);
      }
    });

    group.appendChild(btn);
    input.dataset.revealReady = 'true';
  }

  function enhanceAll(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('input[type="password"]:not([data-no-reveal])');
    for (var i = 0; i < nodes.length; i++) {
      enhance(nodes[i]);
    }
  }

  global.GuestOSPasswordReveal = {
    enhance: enhance,
    enhanceAll: enhanceAll,
    showMs: SHOW_MS,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      enhanceAll(document);
    });
  } else {
    enhanceAll(document);
  }
})(window);
