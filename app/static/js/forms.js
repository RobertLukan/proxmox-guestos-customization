/**
 * Shared helpers for sysprep / reconfigure wizard forms.
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

  function wireWinrm(form) {
    var usePre = form.querySelector('#use_predefined_winrm');
    var fields = form.querySelector('#winrm_credentials_fields');
    var inputs = form.querySelectorAll('#winrm_username, #winrm_password');
    function apply() {
      var use = !usePre || usePre.checked;
      setSectionVisible(fields, !use);
      setDataRequired(Array.prototype.slice.call(inputs), !use);
    }
    if (usePre) usePre.addEventListener('change', apply);
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
    return data;
  }

  function collectReconfigurePayload(form) {
    var usePredefinedWinrm = !!(form.querySelector('#use_predefined_winrm') || {}).checked;
    var useProfileCreds = !!(form.querySelector('#use_domain_profile_credentials') || {}).checked;
    var joinDomain = !!(form.querySelector('#join_domain_checkbox') || {}).checked;
    return {
      vmid: (form.querySelector('#vmid') || {}).value,
      vm_uuid: (form.querySelector('#vm_uuid') || {}).value,
      temp_ip_address: (form.querySelector('#temp_ip_address_hidden') || {}).value,
      primary_mac_address: (form.querySelector('#primary_mac_address_hidden') || {}).value,
      new_ip_address: (form.querySelector('#new_ip_address') || {}).value,
      netmask: (form.querySelector('#netmask') || {}).value,
      gateway: (form.querySelector('#gateway') || {}).value,
      dns_servers: (form.querySelector('#dns_servers') || {}).value,
      use_predefined_winrm: usePredefinedWinrm,
      winrm_username: usePredefinedWinrm ? '' : ((form.querySelector('#winrm_username') || {}).value || ''),
      winrm_password: usePredefinedWinrm ? '' : ((form.querySelector('#winrm_password') || {}).value || ''),
      remove_temp_interface: !!(form.querySelector('#remove_temp_interface') || {}).checked,
      join_domain: joinDomain,
      domain_profile: (form.querySelector('#domain_profile') || {}).value || '',
      use_domain_profile_credentials: useProfileCreds,
      domain_name: useProfileCreds ? '' : ((form.querySelector('#domain_name') || {}).value || ''),
      domain_username: useProfileCreds ? '' : ((form.querySelector('#domain_username') || {}).value || ''),
      domain_password: useProfileCreds ? '' : ((form.querySelector('#domain_password') || {}).value || ''),
      vlan: (form.querySelector('#vlan') || {}).value || '',
    };
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
    var newIp = form.querySelector('#new_ip_address');
    if (newIp) {
      add('New IP', newIp.value);
      add('Netmask', (form.querySelector('#netmask') || {}).value);
      add('Gateway', (form.querySelector('#gateway') || {}).value);
    }
    add('DNS', (form.querySelector('#dns_servers') || {}).value);
    add('VLAN', (form.querySelector('#vlan') || {}).value);
    add('Domain profile', (form.querySelector('#domain_profile') || {}).value);
    var join = form.querySelector('#join_domain_checkbox');
    if (join) add('Join domain', join.checked ? 'Yes' : 'No');
    var winrm = form.querySelector('#use_predefined_winrm');
    if (winrm) add('WinRM credentials', winrm.checked ? 'Predefined' : 'Custom');
    var removeTemp = form.querySelector('#remove_temp_interface');
    if (removeTemp) add('Remove temp NIC', removeTemp.checked ? 'Yes' : 'No');
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
    wireWinrm: wireWinrm,
    collectSysprepPayload: collectSysprepPayload,
    collectReconfigurePayload: collectReconfigurePayload,
    fillReview: fillReview,
    postJson: postJson,
    mapServerErrors: mapServerErrors,
  };
})(window);
