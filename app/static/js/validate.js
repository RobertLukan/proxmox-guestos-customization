/**
 * Client-side validators aligned with app/validators.py.
 * Rules (keep in sync with the server):
 *   - IPv4 dotted quad
 *   - netmask / prefix 0-32
 *   - hostname: 1-15 chars, [A-Za-z0-9-], first DNS label only
 *   - MAC: aa:bb:cc:dd:ee:ff or dash form
 *   - DNS: comma-separated IPv4 list (empty allowed unless required)
 *   - VLAN: empty or 1-4094
 *   - password: min 8 when data-validate="password"
 */
(function (global) {
  'use strict';

  var HOSTNAME_RE = /^[A-Za-z0-9-]{1,15}$/;
  var MAC_RE = /^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$/;

  function isIPv4(value) {
    var parts = String(value).trim().split('.');
    if (parts.length !== 4) return false;
    for (var i = 0; i < 4; i++) {
      if (!/^\d+$/.test(parts[i])) return false;
      var n = Number(parts[i]);
      if (n < 0 || n > 255) return false;
    }
    return true;
  }

  function check(rule, value, el) {
    var v = value == null ? '' : String(value).trim();
    switch (rule) {
      case 'required':
        return v ? null : 'This field is required.';
      case 'ipv4':
        if (!v) return null;
        return isIPv4(v) ? null : 'Enter a valid IPv4 address.';
      case 'netmask':
        if (!v) return null;
        var n = Number(v);
        if (!Number.isInteger(n) || n < 0 || n > 32) return 'Prefix length must be 0–32.';
        return null;
      case 'hostname':
        if (!v) return null;
        var label = v.split('.')[0];
        return HOSTNAME_RE.test(label)
          ? null
          : 'Hostname: 1–15 letters, digits, or hyphens.';
      case 'mac':
        if (!v) return null;
        return MAC_RE.test(v) ? null : 'Enter a valid MAC address.';
      case 'dns_list':
        if (!v) return null;
        var parts = v.split(',');
        for (var i = 0; i < parts.length; i++) {
          var p = parts[i].trim();
          if (!p) continue;
          if (!isIPv4(p)) return 'Each DNS entry must be a valid IPv4 address.';
        }
        return null;
      case 'vlan':
        if (!v) return null;
        var vn = Number(v);
        if (!Number.isInteger(vn) || vn < 1 || vn > 4094) return 'VLAN must be 1–4094.';
        return null;
      case 'password':
        if (!v) return null;
        return v.length >= 8 ? null : 'Password must be at least 8 characters.';
      case 'password_confirm':
        if (!el) return null;
        var otherId = el.getAttribute('data-confirm-of');
        var other = otherId ? document.getElementById(otherId) : null;
        if (!other) return null;
        return v === other.value ? null : 'Passwords do not match.';
      case 'number_min':
        if (!v) return null;
        var min = Number(el && el.getAttribute('min'));
        if (Number(v) < min) return 'Must be at least ' + min + '.';
        return null;
      default:
        return null;
    }
  }

  function rulesFor(el) {
    var raw = el.getAttribute('data-validate') || '';
    var rules = raw.split(/\s+/).filter(Boolean);
    if (el.required || el.getAttribute('data-required') === 'true') {
      if (rules.indexOf('required') === -1) rules.unshift('required');
    }
    if (el.type === 'number' && el.hasAttribute('min') && rules.indexOf('number_min') === -1) {
      rules.push('number_min');
    }
    return rules;
  }

  function setInvalid(el, message) {
    el.classList.add('is-invalid');
    el.classList.remove('is-valid');
    var fb = el.parentElement && el.parentElement.querySelector('.invalid-feedback');
    if (fb) fb.textContent = message || '';
  }

  function setValid(el) {
    el.classList.remove('is-invalid');
    if (el.value) el.classList.add('is-valid');
    else el.classList.remove('is-valid');
    var fb = el.parentElement && el.parentElement.querySelector('.invalid-feedback');
    if (fb) fb.textContent = '';
  }

  function clearValidation(el) {
    el.classList.remove('is-invalid', 'is-valid');
    var fb = el.parentElement && el.parentElement.querySelector('.invalid-feedback');
    if (fb) fb.textContent = '';
  }

  function isEffectivelyVisible(el) {
    if (!el || el.disabled) return false;
    if (el.closest && (el.closest('[hidden]') || el.closest('.d-none'))) return false;
    var node = el;
    while (node && node.nodeType === 1) {
      var style = window.getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      node = node.parentElement;
    }
    return true;
  }

  function validateField(el) {
    if (el.disabled || el.getAttribute('data-skip-validate') === 'true') {
      clearValidation(el);
      return true;
    }
    // Skip fields in hidden wizard panels / collapsed sections
    if (!isEffectivelyVisible(el)) {
      clearValidation(el);
      return true;
    }
    var rules = rulesFor(el);
    for (var i = 0; i < rules.length; i++) {
      var msg = check(rules[i], el.value, el);
      if (msg) {
        setInvalid(el, msg);
        return false;
      }
    }
    setValid(el);
    return true;
  }

  function fieldsIn(root) {
    return Array.prototype.slice.call(
      root.querySelectorAll('input, select, textarea')
    ).filter(function (el) {
      return el.type !== 'hidden' && el.type !== 'submit' && el.type !== 'button';
    });
  }

  function validateStep(root) {
    var ok = true;
    fieldsIn(root).forEach(function (el) {
      // Mark required dynamically based on visibility / data-required
      if (!validateField(el)) ok = false;
    });
    return ok;
  }

  function validateForm(form) {
    return validateStep(form);
  }

  function bindLive(root) {
    fieldsIn(root).forEach(function (el) {
      el.addEventListener('blur', function () {
        el.dataset.touched = '1';
        validateField(el);
      });
      el.addEventListener('input', function () {
        if (el.dataset.touched === '1') validateField(el);
        root.dispatchEvent(new CustomEvent('gos:field-change', { bubbles: true }));
      });
      el.addEventListener('change', function () {
        el.dataset.touched = '1';
        validateField(el);
        root.dispatchEvent(new CustomEvent('gos:field-change', { bubbles: true }));
      });
    });
  }

  global.GuestOSValidate = {
    isIPv4: isIPv4,
    check: check,
    validateField: validateField,
    validateStep: validateStep,
    validateForm: validateForm,
    bindLive: bindLive,
    clearValidation: clearValidation,
    setInvalid: setInvalid,
  };
})(window);
