/**
 * Shared helpers for the Sysprep wizard form.
 * Payload field names must stay aligned with start_* routes.
 */
(function (global) {
  'use strict';

  function showFormAlert(el, message, fieldErrors) {
    if (!el) return;
    el.className = 'alert alert-danger';
    var parts = [];
    if (message) parts.push(message);
    if (fieldErrors && typeof fieldErrors === 'object') {
      Object.keys(fieldErrors).forEach(function (k) {
        parts.push(k + ': ' + fieldErrors[k]);
      });
    }
    el.textContent = parts.join(' ') || 'Request failed.';
  }

  function clearFormAlert(el) {
    if (!el) return;
    el.textContent = '';
    el.className = 'alert alert-danger';
  }

  function applyDomainProfile(selectEl, profiles, dnsEl, vlanEl) {
    var name = selectEl && selectEl.value;
    if (name && profiles && profiles[name]) {
      var p = profiles[name];
      if (dnsEl && p.dns_servers !== undefined) dnsEl.value = p.dns_servers || '';
      if (vlanEl && p.vlan !== undefined && p.vlan !== null && p.vlan !== '') {
        vlanEl.value = p.vlan;
      }
    } else {
      if (dnsEl) dnsEl.value = '';
      if (vlanEl) vlanEl.value = '';
    }
  }

  function setSectionVisible(el, visible) {
    if (!el) return;
    el.style.display = visible ? '' : 'none';
    el.hidden = !visible;
  }

  function setDataRequired(els, on) {
    (els || []).forEach(function (el) {
      if (!el) return;
      if (on) el.setAttribute('data-required', 'true');
      else {
        el.removeAttribute('data-required');
        if (window.GuestOSValidate) GuestOSValidate.clearValidation(el);
      }
    });
  }

  function wireNetworkMode(form) {
    var mode = form.querySelector('#network_mode');
    var staticFields = form.querySelector('#static_fields');
    var staticInputs = form.querySelectorAll('#ip_address, #netmask_cidr, #gateway, #new_ip_address, #netmask');
    function apply() {
      var isDhcp = mode && mode.value === 'dhcp';
      setSectionVisible(staticFields, !isDhcp);
      setDataRequired(Array.prototype.slice.call(staticInputs), !isDhcp);
    }
    if (mode) mode.addEventListener('change', apply);
    apply();
    return apply;
  }

  function wireDomainJoin(form) {
    var joinCb = form.querySelector('#join_domain_checkbox');
    var domainFields = form.querySelector('#domain_fields');
    var useProfileCb = form.querySelector('#use_domain_profile_credentials');
    var credFields = form.querySelector('#domain_credentials_fields');
    var profileSelect = form.querySelector('#domain_profile');
    var manualCreds = form.querySelectorAll('#domain_name, #domain_username, #domain_password');

    function applyCreds() {
      var joining = joinCb && joinCb.checked;
      var useProfile = !useProfileCb || useProfileCb.checked;
      setSectionVisible(credFields, joining && !useProfile);
      setDataRequired(Array.prototype.slice.call(manualCreds), joining && !useProfile);
      if (profileSelect) {
        if (joining && useProfile) profileSelect.setAttribute('data-required', 'true');
        else profileSelect.removeAttribute('data-required');
      }
    }

    function applyJoin() {
      var joining = joinCb && joinCb.checked;
      setSectionVisible(domainFields, joining);
      applyCreds();
    }

    if (joinCb) joinCb.addEventListener('change', applyJoin);
    if (useProfileCb) useProfileCb.addEventListener('change', applyCreds);
    applyJoin();
    return { applyJoin: applyJoin, applyCreds: applyCreds };
  }

  function wireManageDisks(form) {
    var manageCb = form.querySelector('#manage_disks_checkbox');
    var diskFields = form.querySelector('#disk_fields');
    var pagefileCb = form.querySelector('#ensure_pagefile_disk');
    var dataCb = form.querySelector('#ensure_data_disk');
    var pagefileFields = form.querySelector('#pagefile_disk_fields');
    var dataFields = form.querySelector('#data_disk_fields');

    function apply() {
      var on = manageCb && manageCb.checked;
      setSectionVisible(diskFields, on);
      setSectionVisible(pagefileFields, on && (!pagefileCb || pagefileCb.checked));
      setSectionVisible(dataFields, on && (!dataCb || dataCb.checked));
    }
    if (manageCb) manageCb.addEventListener('change', apply);
    if (pagefileCb) pagefileCb.addEventListener('change', apply);
    if (dataCb) dataCb.addEventListener('change', apply);
    apply();
    return apply;
  }

  function collectSysprepPayload(form) {
    var data = Object.fromEntries(new FormData(form).entries());
    var joinDomain = !!(form.querySelector('#join_domain_checkbox') || {}).checked;
    var useProfileCreds = !!(form.querySelector('#use_domain_profile_credentials') || {}).checked;
    data.join_domain = joinDomain;
    data.use_domain_profile_credentials = useProfileCreds;
    if (!joinDomain || useProfileCreds) {
      data.domain_name = '';
      data.domain_username = '';
      data.domain_password = '';
    }

    var manageDisks = !!(form.querySelector('#manage_disks_checkbox') || {}).checked;
    data.manage_disks = manageDisks;
    delete data.os_grow_to_gb;
    delete data.pagefile_size_gb;
    delete data.pagefile_drive_letter;
    delete data.data_size_gb;
    delete data.data_drive_letter;
    if (manageDisks) {
      var disks = [{ role: 'os' }];
      var grow = (form.querySelector('#os_grow_to_gb') || {}).value;
      if (grow) disks[0].grow_to_gb = parseInt(grow, 10);
      if ((form.querySelector('#ensure_pagefile_disk') || {}).checked) {
        disks.push({
          role: 'pagefile',
          size_gb: parseInt((form.querySelector('#pagefile_size_gb') || {}).value || '16', 10),
          drive_letter: ((form.querySelector('#pagefile_drive_letter') || {}).value || 'P'),
          ensure_pagefile: true,
        });
      }
      if ((form.querySelector('#ensure_data_disk') || {}).checked) {
        disks.push({
          role: 'data',
          size_gb: parseInt((form.querySelector('#data_size_gb') || {}).value || '50', 10),
          drive_letter: ((form.querySelector('#data_drive_letter') || {}).value || 'D'),
          label: 'Data',
        });
      }
      data.disks = disks;
    } else {
      data.disks = [];
    }
    return data;
  }

  function fillReview(form, target) {
    if (!target) return;
    var rows = [];
    function add(label, value) {
      rows.push('<dt>' + label + '</dt><dd>' + (value || '—') + '</dd>');
    }
    var hostname = form.querySelector('#hostname');
    if (hostname) add('Hostname', hostname.value);
    var remote = form.querySelector('#remote_id, [name="remote_id"]');
    if (remote && remote.value) add('Remote', remote.value);
    var cores = form.querySelector('#cores');
    if (cores) add('CPU cores', cores.value);
    var ram = form.querySelector('#ram');
    if (ram) add('RAM (MB)', ram.value);
    var bridge = form.querySelector('#bridge');
    if (bridge) add('Bridge', bridge.value);
    var mode = form.querySelector('#network_mode');
    if (mode) {
      add('Network mode', mode.value);
      if (mode.value === 'static') {
        add('IP', (form.querySelector('#ip_address') || {}).value);
        add('Netmask', (form.querySelector('#netmask_cidr') || {}).value);
        add('Gateway', (form.querySelector('#gateway') || {}).value);
      }
    }
    add('DNS', (form.querySelector('#dns_servers') || {}).value);
    add('VLAN', (form.querySelector('#vlan') || {}).value);
    add('Domain profile', (form.querySelector('#domain_profile') || {}).value);
    var join = form.querySelector('#join_domain_checkbox');
    if (join) add('Join domain', join.checked ? 'Yes' : 'No');
    var manageDisks = form.querySelector('#manage_disks_checkbox');
    if (manageDisks) {
      add('Configure disks', manageDisks.checked ? 'Yes' : 'No');
      if (manageDisks.checked) {
        add('Grow OS (GB)', (form.querySelector('#os_grow_to_gb') || {}).value);
        if ((form.querySelector('#ensure_pagefile_disk') || {}).checked) {
          add(
            'Pagefile disk',
            ((form.querySelector('#pagefile_size_gb') || {}).value || '') +
              'G → ' +
              ((form.querySelector('#pagefile_drive_letter') || {}).value || 'P')
          );
        }
        if ((form.querySelector('#ensure_data_disk') || {}).checked) {
          add(
            'Data disk',
            ((form.querySelector('#data_size_gb') || {}).value || '') +
              'G → ' +
              ((form.querySelector('#data_drive_letter') || {}).value || 'D')
          );
        }
      }
    }
    target.innerHTML = rows.join('');
  }

  async function postJson(url, payload) {
    var response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    var body = {};
    try {
      body = await response.json();
    } catch (e) {
      body = {};
    }
    return { ok: response.ok, status: response.status, body: body };
  }

  function mapServerErrors(form, errors) {
    if (!errors || typeof errors !== 'object') return;
    Object.keys(errors).forEach(function (name) {
      var el = form.querySelector('[name="' + name + '"], #' + name);
      if (el && window.GuestOSValidate) GuestOSValidate.setInvalid(el, errors[name]);
    });
  }

  global.GuestOSForms = {
    showFormAlert: showFormAlert,
    clearFormAlert: clearFormAlert,
    applyDomainProfile: applyDomainProfile,
    wireNetworkMode: wireNetworkMode,
    wireDomainJoin: wireDomainJoin,
    wireManageDisks: wireManageDisks,
    collectSysprepPayload: collectSysprepPayload,
    fillReview: fillReview,
    postJson: postJson,
    mapServerErrors: mapServerErrors,
  };
})(window);
