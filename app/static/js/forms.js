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

  function applyDomainProfile(selectEl, profiles, dnsEl, vlanEl, opts) {
    var name = selectEl && selectEl.value;
    opts = opts || {};
    var overwrite = !!opts.overwrite;
    if (!(name && profiles && profiles[name])) {
      return;
    }
    var p = profiles[name];
    // UI profile picks overwrite DNS/VLAN; fill-blank is for specs / silent apply.
    if (dnsEl && p.dns_servers !== undefined && p.dns_servers !== null) {
      if (overwrite || !(dnsEl.value || '').trim()) {
        dnsEl.value = p.dns_servers || '';
      }
    }
    if (vlanEl) {
      var hasVlan = p.vlan !== undefined && p.vlan !== null && p.vlan !== '';
      if (hasVlan && (overwrite || !(vlanEl.value || '').trim())) {
        vlanEl.value = p.vlan;
      } else if (overwrite && !hasVlan) {
        vlanEl.value = '';
      }
    }
  }

  function wireDomainProfileNetwork(form, profiles) {
    var useNetCb = form.querySelector('#use_domain_profile_network');
    var profileGroup = form.querySelector('#domain_profile_group');
    var profileSelect = form.querySelector('#domain_profile');
    var dnsEl = form.querySelector('#dns_servers');
    var vlanEl = form.querySelector('#vlan');
    var credHint = form.querySelector('#domain_profile_cred_hint');
    form._guestosDomainProfiles = profiles || {};

    function updateCredHint() {
      if (!credHint) return;
      var name = (profileSelect && profileSelect.value) || '';
      if (name) {
        credHint.textContent =
          'Credentials will come from network profile “' + name + '” (server-side).';
      } else {
        credHint.textContent =
          'Select a domain profile under Network to use profile credentials, or uncheck below and type them.';
      }
    }

    function setDnsVlanLocked(locked) {
      [dnsEl, vlanEl].forEach(function (el) {
        if (!el) return;
        // readOnly (not disabled) so FormData / payload collection still includes values.
        el.readOnly = !!locked;
        el.tabIndex = locked ? -1 : 0;
        if (locked) {
          el.classList.add('bg-light');
          el.setAttribute('aria-readonly', 'true');
        } else {
          el.classList.remove('bg-light');
          el.removeAttribute('aria-readonly');
        }
      });
    }

    function syncProfileNetwork() {
      var on = !!(useNetCb && useNetCb.checked);
      setSectionVisible(profileGroup, on);
      if (!on && profileSelect) {
        var joining = form.querySelector('#join_domain_checkbox');
        var useCreds = form.querySelector('#use_domain_profile_credentials');
        var provisionMode = form.querySelector('#provision_mode');
        var bulk = provisionMode && provisionMode.value === 'bulk';
        var keepForJoin =
          bulk ||
          (joining && joining.checked && (!useCreds || useCreds.checked));
        if (!keepForJoin) {
          profileSelect.value = '';
        }
        profileSelect.removeAttribute('data-required');
      }
      if (on && profileSelect && profileSelect.value) {
        applyDomainProfile(profileSelect, profiles || {}, dnsEl, vlanEl, {
          overwrite: true,
        });
      }
      // Lock DNS/VLAN whenever profile mode is on (values come from the profile).
      setDnsVlanLocked(on);
      updateCredHint();
    }

    if (useNetCb) useNetCb.addEventListener('change', syncProfileNetwork);
    if (profileSelect) {
      profileSelect.addEventListener('change', function () {
        if (useNetCb && useNetCb.checked) {
          applyDomainProfile(profileSelect, profiles || {}, dnsEl, vlanEl, {
            overwrite: true,
          });
        }
        updateCredHint();
      });
    }
    syncProfileNetwork();
    return { applyToggle: syncProfileNetwork, updateCredHint: updateCredHint };
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
    var workgroupGroup = form.querySelector('#workgroup_group');
    var useProfileCb = form.querySelector('#use_domain_profile_credentials');
    var credFields = form.querySelector('#domain_credentials_fields');
    var profileSelect = form.querySelector('#domain_profile');
    var profileJoinSelect = form.querySelector('#domain_profile_join');
    var profileJoinGroup = form.querySelector('#domain_profile_join_group');
    var useNetCb = form.querySelector('#use_domain_profile_network');
    var useNetGroup = form.querySelector('#use_domain_profile_network_group');
    var credHint = form.querySelector('#domain_profile_cred_hint');
    var credHelp = form.querySelector('#domain_profile_cred_help');
    var manualCreds = form.querySelectorAll('#domain_name, #domain_username, #domain_password, #domain_password_confirm');
    var syncingProfile = false;
    var testManualBtn = form.querySelector('#test_domain_credentials_btn');
    var testManualStatus = form.querySelector('#test_domain_credentials_status');
    var testProfileBtn = form.querySelector('#test_domain_profile_btn');
    var testProfileStatus = form.querySelector('#test_domain_profile_status');
    var profileTestGroup = form.querySelector('#domain_profile_test_group');
    var continueNote = form.querySelector('#domain_cred_test_continue_note');

    function isBulkMode() {
      var mode = form.querySelector('#provision_mode');
      return !!(mode && mode.value === 'bulk');
    }

    function networkDnsServers() {
      if (isBulkMode()) {
        var bulkDns = form.querySelector('#bulk_dns_servers');
        if (bulkDns && String(bulkDns.value || '').trim()) {
          return String(bulkDns.value).trim();
        }
      }
      var dnsEl = form.querySelector('#dns_servers');
      return dnsEl ? String(dnsEl.value || '').trim() : '';
    }

    function setTestStatus(el, kind, text) {
      // kind: 'ok' | 'fail' | 'warn' | null (neutral)
      if (!el) return;
      el.textContent = text || '';
      el.classList.remove('text-success', 'text-danger', 'text-warning');
      if (kind === 'ok') el.classList.add('text-success');
      else if (kind === 'fail') el.classList.add('text-danger');
      else if (kind === 'warn') el.classList.add('text-warning');
    }

    function setContinueNote(visible) {
      if (!continueNote) return;
      continueNote.hidden = !visible;
    }

    function postCredentialTest(body, statusEl, btn) {
      if (btn) btn.disabled = true;
      setContinueNote(false);
      setTestStatus(statusEl, null, 'Testing…');
      return fetch('/api/domain/test_credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin',
      }).then(function (resp) {
        return resp.json().then(function (j) {
          return { httpOk: resp.ok, ok: resp.ok && j && j.ok, body: j || {} };
        }).catch(function () {
          return { httpOk: resp.ok, ok: false, body: {} };
        });
      }).then(function (r) {
        if (r.ok) {
          form.dataset.domainCredTest = 'ok';
          setContinueNote(false);
          var bindBit =
            'OK — bind succeeded' +
            (r.body.bind_target ? ' (' + r.body.bind_target + ')' : '');
          if (r.body.ou_warning) {
            setTestStatus(statusEl, 'warn', bindBit + '. ' + r.body.ou_warning);
          } else if (r.body.domain_ou) {
            setTestStatus(statusEl, 'ok', bindBit + '; OU ' + r.body.domain_ou);
          } else {
            setTestStatus(statusEl, 'ok', bindBit);
          }
        } else {
          form.dataset.domainCredTest = 'failed';
          setContinueNote(true);
          var msg =
            (r.body && (r.body.result || r.body.message || r.body.error)) ||
            'Credential test failed';
          if (r.body && r.body.class) {
            msg = '[' + r.body.class + '] ' + msg;
          }
          setTestStatus(
            statusEl,
            'warn',
            'Could not validate — you can continue. ' + String(msg).split('\n')[0]
          );
        }
      }).catch(function (err) {
        form.dataset.domainCredTest = 'failed';
        setContinueNote(true);
        setTestStatus(
          statusEl,
          'warn',
          'Could not validate — you can continue. Test request failed: ' +
            (err && err.message ? err.message : err)
        );
      }).finally(function () {
        if (btn) btn.disabled = false;
      });
    }

    function syncProfileSelects(source) {
      if (syncingProfile) return;
      syncingProfile = true;
      try {
        var val = (source && source.value) || '';
        if (profileSelect && source !== profileSelect) profileSelect.value = val;
        if (profileJoinSelect && source !== profileJoinSelect) profileJoinSelect.value = val;
      } finally {
        syncingProfile = false;
      }
    }

    function applyCreds() {
      var joining = joinCb && joinCb.checked;
      var useProfile = !useProfileCb || useProfileCb.checked;
      var bulk = isBulkMode();
      setSectionVisible(credFields, joining && !useProfile);
      setDataRequired(Array.prototype.slice.call(manualCreds), joining && !useProfile);
      setSectionVisible(profileJoinGroup, bulk && joining && useProfile);
      setSectionVisible(profileTestGroup, joining && useProfile);
      if (profileJoinSelect) {
        if (bulk && joining && useProfile) {
          profileJoinSelect.setAttribute('data-required', 'true');
        } else {
          profileJoinSelect.removeAttribute('data-required');
          if (window.GuestOSValidate) GuestOSValidate.clearValidation(profileJoinSelect);
        }
      }
      if (profileSelect) {
        if (joining && useProfile) {
          profileSelect.setAttribute('data-required', 'true');
          // Single customize: ensure Network profile UI is visible when join needs a profile.
          // Bulk: DNS/VLAN come from Basics/CSV — do not enable the Network DNS/VLAN shortcut.
          if (!bulk && useNetCb && !useNetCb.checked) {
            useNetCb.checked = true;
            useNetCb.dispatchEvent(new Event('change', { bubbles: true }));
          }
        } else {
          profileSelect.removeAttribute('data-required');
        }
      }
      if (credHint) {
        if (bulk) {
          credHint.textContent = useProfile
            ? 'Pick a domain profile below for join credentials. DNS is optional on Basics (DHCP when blank).'
            : 'Enter domain credentials below, or check “Use Domain Profile Credentials”.';
        } else if (useProfile) {
          credHint.textContent =
            'Select a domain profile under Network to use profile credentials, or uncheck below and type them.';
        }
      }
      if (credHelp) {
        credHelp.textContent = bulk
          ? 'Uses the selected profile for join credentials only (server-side). Does not set DNS or VLAN.'
          : 'Uses the same profile chosen under Network for DNS/VLAN. Credentials stay on the server.';
      }
    }

    function applyJoin() {
      var joining = joinCb && joinCb.checked;
      setSectionVisible(domainFields, joining);
      setSectionVisible(workgroupGroup, !joining);
      applyCreds();
    }

    if (joinCb) joinCb.addEventListener('change', applyJoin);
    if (useProfileCb) useProfileCb.addEventListener('change', applyCreds);
    if (profileJoinSelect) {
      profileJoinSelect.addEventListener('change', function () {
        syncProfileSelects(profileJoinSelect);
        if (profileSelect) {
          profileSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }
      });
    }
    if (profileSelect) {
      profileSelect.addEventListener('change', function () {
        syncProfileSelects(profileSelect);
      });
    }
    if (testManualBtn) {
      testManualBtn.addEventListener('click', function () {
        var dns = networkDnsServers();
        if (!dns) {
          setContinueNote(false);
          setTestStatus(
            testManualStatus,
            'warn',
            'Set DNS servers on the Network step first (DC or domain DNS IPs). Those addresses are used for this test.'
          );
          return;
        }
        postCredentialTest({
          use_domain_profile_credentials: false,
          domain_name: (form.querySelector('#domain_name') || {}).value || '',
          domain_username: (form.querySelector('#domain_username') || {}).value || '',
          domain_password: (form.querySelector('#domain_password') || {}).value || '',
          dns_servers: dns,
          domain_ou: (form.querySelector('#domain_ou') || {}).value || '',
        }, testManualStatus, testManualBtn);
      });
    }
    if (testProfileBtn) {
      testProfileBtn.addEventListener('click', function () {
        var name = '';
        if (isBulkMode() && profileJoinSelect && profileJoinSelect.value) {
          name = profileJoinSelect.value;
        } else if (profileSelect) {
          name = profileSelect.value || '';
        }
        if (!name) {
          setContinueNote(false);
          setTestStatus(testProfileStatus, 'fail', 'Select a domain profile first.');
          return;
        }
        var dns = networkDnsServers();
        if (!dns) {
          setContinueNote(false);
          setTestStatus(
            testProfileStatus,
            'warn',
            'No Network DNS set — will try the profile’s DNS if configured. Prefer setting DNS on the Network step.'
          );
        }
        postCredentialTest({
          use_domain_profile_credentials: true,
          domain_profile: name,
          dns_servers: dns,
          domain_ou: (form.querySelector('#domain_ou') || {}).value || '',
        }, testProfileStatus, testProfileBtn);
      });
    }
    applyJoin();
    return {
      applyJoin: applyJoin,
      applyCreds: applyCreds,
      isBulkMode: isBulkMode,
      useNetGroup: useNetGroup,
    };
  }

  function wireIpv6(form) {
    var cb = form.querySelector('#enable_ipv6');
    var box = form.querySelector('#ipv6_fields');
    function apply() {
      setSectionVisible(box, !!(cb && cb.checked));
    }
    if (cb) cb.addEventListener('change', apply);
    apply();
    return apply;
  }

  function wireExtraNics(form) {
    var list = form.querySelector('#extra_nics_list');
    var addBtn = form.querySelector('#add_nic_btn');
    var section = form.querySelector('#extra_nics_section');
    var bridges = [];
    var bridgeSelect = form.querySelector('#bridge');
    if (bridgeSelect) {
      bridges = Array.prototype.map.call(bridgeSelect.options, function (o) { return o.value; });
    }
    var idx = 0;
    function addNic(preset) {
      if (!list) return;
      preset = preset || {};
      idx += 1;
      var wrap = document.createElement('div');
      wrap.className = 'border rounded p-3 mb-3 extra-nic';
      var bridgeOpts = bridges.map(function (b) {
        var sel = (preset.bridge || (bridgeSelect && bridgeSelect.value) || '') === b ? ' selected' : '';
        return '<option value="' + b + '"' + sel + '>' + b + '</option>';
      }).join('');
      wrap.innerHTML =
        '<div class="d-flex justify-content-between align-items-center mb-2">' +
        '<strong>Additional NIC</strong>' +
        '<button type="button" class="btn btn-sm btn-outline-danger remove-nic">Remove</button></div>' +
        '<div class="row">' +
        '<div class="col-md-4 mb-2"><label class="form-label">Bridge</label>' +
        '<select class="form-select nic-bridge">' + bridgeOpts + '</select></div>' +
        '<div class="col-md-4 mb-2"><label class="form-label">VLAN</label>' +
        '<input type="number" class="form-control nic-vlan" min="1" max="4094" value="' + (preset.vlan || '') + '"></div>' +
        '<div class="col-md-4 mb-2"><label class="form-label">Mode</label>' +
        '<select class="form-select nic-mode"><option value="static">Static</option><option value="dhcp">DHCP</option></select></div>' +
        '</div>' +
        '<div class="nic-static row">' +
        '<div class="col-md-4 mb-2"><label class="form-label">IP</label><input class="form-control nic-ip" value="' + (preset.ip_address || '') + '"></div>' +
        '<div class="col-md-4 mb-2"><label class="form-label">Prefix</label><input class="form-control nic-prefix" value="' + (preset.netmask_cidr || '24') + '"></div>' +
        '<div class="col-md-4 mb-2"><label class="form-label">Gateway (optional)</label><input class="form-control nic-gw" value="' + (preset.gateway || '') + '" placeholder="omit on secondary NICs"></div>' +
        '</div>' +
        '<div class="mb-2"><label class="form-label">DNS</label><input class="form-control nic-dns" value="' + (preset.dns_servers || '') + '"></div>' +
        '<div class="form-check mb-2"><input type="checkbox" class="form-check-input nic-ipv6">' +
        '<label class="form-check-label">Enable IPv6</label></div>' +
        '<div class="nic-ipv6-fields row" hidden>' +
        '<div class="col-md-4 mb-2"><label class="form-label">IPv6</label><input class="form-control nic-ip6" value="' + (preset.ipv6_address || '') + '"></div>' +
        '<div class="col-md-4 mb-2"><label class="form-label">Prefix</label><input class="form-control nic-prefix6" value="' + (preset.ipv6_prefix || '64') + '"></div>' +
        '<div class="col-md-4 mb-2"><label class="form-label">GW</label><input class="form-control nic-gw6" value="' + (preset.ipv6_gateway || '') + '"></div>' +
        '</div>';
      list.appendChild(wrap);
      var mode = wrap.querySelector('.nic-mode');
      if (preset.network_mode === 'dhcp') mode.value = 'dhcp';
      var ipv6Cb = wrap.querySelector('.nic-ipv6');
      if (preset.enable_ipv6) ipv6Cb.checked = true;
      function syncMode() {
        wrap.querySelector('.nic-static').hidden = mode.value === 'dhcp';
      }
      function syncIpv6() {
        wrap.querySelector('.nic-ipv6-fields').hidden = !ipv6Cb.checked;
      }
      mode.addEventListener('change', syncMode);
      ipv6Cb.addEventListener('change', syncIpv6);
      wrap.querySelector('.remove-nic').addEventListener('click', function () { wrap.remove(); });
      syncMode();
      syncIpv6();
    }
    if (addBtn) addBtn.addEventListener('click', function () { addNic(); });
    function setVisible(show) {
      setSectionVisible(section, show);
    }
    return { addNic: addNic, setVisible: setVisible };
  }

  function collectExtraNics(form) {
    var cards = form.querySelectorAll('.extra-nic');
    var nics = [];
    cards.forEach(function (wrap) {
      var mode = (wrap.querySelector('.nic-mode') || {}).value || 'static';
      var nic = {
        bridge: (wrap.querySelector('.nic-bridge') || {}).value || '',
        vlan: (wrap.querySelector('.nic-vlan') || {}).value || '',
        network_mode: mode,
        dns_servers: (wrap.querySelector('.nic-dns') || {}).value || '',
        enable_ipv6: !!(wrap.querySelector('.nic-ipv6') || {}).checked,
      };
      if (mode === 'static') {
        nic.ip_address = (wrap.querySelector('.nic-ip') || {}).value || '';
        nic.netmask_cidr = (wrap.querySelector('.nic-prefix') || {}).value || '';
        nic.gateway = (wrap.querySelector('.nic-gw') || {}).value || '';
      }
      if (nic.enable_ipv6) {
        nic.ipv6_address = (wrap.querySelector('.nic-ip6') || {}).value || '';
        nic.ipv6_prefix = (wrap.querySelector('.nic-prefix6') || {}).value || '64';
        nic.ipv6_gateway = (wrap.querySelector('.nic-gw6') || {}).value || '';
      }
      nics.push(nic);
    });
    return nics;
  }

  function applySpecPayload(form, payload) {
    if (!payload || typeof payload !== 'object') return;
    function setVal(sel, val) {
      var el = form.querySelector(sel);
      if (!el || val === undefined || val === null || val === '') return;
      if (el.type === 'checkbox') {
        el.checked = !!val && val !== 'false' && val !== '0';
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return;
      }
      el.value = val;
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
    setVal('#timezone', payload.timezone);
    setVal('#locale', payload.locale);
    setVal('#workgroup', payload.workgroup);
    setVal('#cores', payload.cores);
    if (payload.ram !== undefined && payload.ram !== null && payload.ram !== '') {
      var ramMb = Number(payload.ram);
      if (!isNaN(ramMb) && ramMb > 0) {
        setVal('#ram_gb', Math.max(1, Math.round(ramMb / 1024)));
      }
    } else if (payload.ram_gb !== undefined) {
      setVal('#ram_gb', payload.ram_gb);
    }
    setVal('#bridge', payload.bridge);
    setVal('#vlan', payload.vlan);
    setVal('#network_mode', payload.network_mode);
    setVal('#ip_address', payload.ip_address);
    setVal('#netmask_cidr', payload.netmask_cidr);
    setVal('#gateway', payload.gateway);
    setVal('#dns_servers', payload.dns_servers);
    if (payload.enable_ipv6 !== undefined) setVal('#enable_ipv6', payload.enable_ipv6);
    setVal('#ipv6_address', payload.ipv6_address);
    setVal('#ipv6_prefix', payload.ipv6_prefix);
    setVal('#ipv6_gateway', payload.ipv6_gateway);
    setVal('#domain_profile', payload.domain_profile);
    var useNetCb = form.querySelector('#use_domain_profile_network');
    if (useNetCb) {
      useNetCb.checked = !!(payload.domain_profile || '').toString().trim();
      useNetCb.dispatchEvent(new Event('change', { bubbles: true }));
    }
    var profileSelect = form.querySelector('#domain_profile');
    var dnsEl = form.querySelector('#dns_servers');
    var vlanEl = form.querySelector('#vlan');
    // Spec may only store domain_profile; fill blank DNS/VLAN without clobbering
    // explicit values already applied from the payload.
    if (profileSelect && (payload.domain_profile || '').toString().trim()) {
      applyDomainProfile(
        profileSelect,
        form._guestosDomainProfiles || {},
        dnsEl,
        vlanEl,
        { overwrite: false }
      );
    }
    setVal('#domain_ou', payload.domain_ou);
    setVal('#domain_name', payload.domain_name);
    var joinCb = form.querySelector('#join_domain_checkbox');
    if (joinCb && payload.join_domain !== undefined) {
      joinCb.checked = !!payload.join_domain;
      joinCb.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  function wireApplySpec(form) {
    var sel = form.querySelector('#apply_spec');
    if (!sel) return;
    sel.addEventListener('change', function () {
      var opt = sel.options[sel.selectedIndex];
      if (!opt || !opt.value) return;
      var raw = opt.getAttribute('data-payload');
      if (!raw) return;
      try {
        applySpecPayload(form, JSON.parse(raw));
      } catch (e) {
        console.warn('Failed to apply spec payload', e);
      }
    });
  }

  function wireManageDisks(form) {
    var manageCb = form.querySelector('#manage_disks_checkbox');
    var diskFields = form.querySelector('#disk_fields');
    var statusEl = form.querySelector('#disk_planner_status');
    var bodyEl = form.querySelector('#disk_planner_body');
    var swapBtn = form.querySelector('#disk_planner_swap');
    var addPfBtn = form.querySelector('#disk_planner_add_pagefile');
    var addDataBtn = form.querySelector('#disk_planner_add_data');
    var reloadBtn = form.querySelector('#disk_planner_reload');
    var templateInput = form.querySelector('[name="template_vmid"]');
    var remoteInput = form.querySelector('[name="remote_id"]');

    var state = {
      loaded: false,
      loading: false,
      inventory: [],
      rows: [], // { kind:'boot'|'existing'|'new', key, currentGb, role, targetGb, letter, selected }
    };
    form._diskPlanner = state;

    function setStatus(msg) {
      if (statusEl) statusEl.textContent = msg || '';
    }

    function defaultLetter(role, used) {
      var prefer = role === 'pagefile' ? 'P' : 'D';
      if (used.indexOf(prefer) === -1) return prefer;
      var alphabet = 'DEFGHIJKLMNOPQRSTUVWXYZ';
      for (var i = 0; i < alphabet.length; i++) {
        if (used.indexOf(alphabet[i]) === -1) return alphabet[i];
      }
      return prefer;
    }

    function suggestRoles(disks) {
      var secondaries = disks.filter(function (d) { return !d.is_boot; })
        .slice()
        .sort(function (a, b) {
          return (a.size_gb || 0) - (b.size_gb || 0);
        });
      var map = {};
      if (secondaries.length === 1) {
        map[secondaries[0].key] = 'data';
      } else if (secondaries.length >= 2) {
        map[secondaries[0].key] = 'pagefile';
        map[secondaries[secondaries.length - 1].key] = 'data';
      }
      return map;
    }

    function rebuildRowsFromInventory(invDisks) {
      var suggest = suggestRoles(invDisks);
      var usedLetters = [];
      state.rows = invDisks.map(function (d) {
        if (d.is_boot) {
          return {
            kind: 'boot',
            key: d.key,
            currentGb: d.size_gb || 0,
            role: 'os',
            targetGb: d.size_gb || 0,
            letter: 'C',
            selected: false,
            serial: d.serial || '',
          };
        }
        var role = suggest[d.key] || 'leave';
        var letter = '';
        if (role === 'pagefile' || role === 'data') {
          letter = defaultLetter(role, usedLetters);
          usedLetters.push(letter);
        }
        return {
          kind: 'existing',
          key: d.key,
          currentGb: d.size_gb || 0,
          role: role,
          targetGb: d.size_gb || 0,
          letter: letter,
          selected: false,
          serial: d.serial || '',
        };
      });
    }

    function readDomIntoState() {
      if (!bodyEl) return;
      var trs = bodyEl.querySelectorAll('tr[data-planner-idx]');
      trs.forEach(function (tr) {
        var idx = parseInt(tr.getAttribute('data-planner-idx'), 10);
        var row = state.rows[idx];
        if (!row) return;
        var roleEl = tr.querySelector('.planner-role');
        var targetEl = tr.querySelector('.planner-target');
        var letterEl = tr.querySelector('.planner-letter');
        var selEl = tr.querySelector('.planner-select');
        if (roleEl && row.kind !== 'boot') row.role = roleEl.value;
        if (targetEl) {
          var t = parseInt(targetEl.value, 10);
          if (!isNaN(t)) row.targetGb = t;
        }
        if (letterEl && row.kind !== 'boot') {
          row.letter = (letterEl.value || '').toUpperCase().slice(0, 1);
        }
        if (selEl) row.selected = !!selEl.checked;
      });
    }

    function render() {
      if (!bodyEl) return;
      var html = [];
      state.rows.forEach(function (row, idx) {
        var currentLabel = row.currentGb ? (row.currentGb + ' GB') : '—';
        var keyLabel = row.kind === 'new' ? '(new)' : row.key;
        var serialBit = row.serial ? (' <span class="text-muted">serial=' + row.serial + '</span>') : '';
        if (row.kind === 'boot') {
          html.push(
            '<tr data-planner-idx="' + idx + '">' +
            '<td></td>' +
            '<td><code>' + keyLabel + '</code>' + serialBit + '</td>' +
            '<td>' + currentLabel + '</td>' +
            '<td>OS (boot)</td>' +
            '<td><input type="number" class="form-control form-control-sm planner-target" min="' +
              (row.currentGb || 1) + '" value="' + (row.targetGb || row.currentGb || '') +
              '" title="Grow OS to GB (min = current)"></td>' +
            '<td>C</td></tr>'
          );
          return;
        }
        var roleOpts = [
          ['leave', 'Leave as-is'],
          ['data', 'Data'],
          ['pagefile', 'Pagefile'],
        ].map(function (opt) {
          return '<option value="' + opt[0] + '"' +
            (row.role === opt[0] ? ' selected' : '') + '>' + opt[1] + '</option>';
        }).join('');
        var disabledSize = row.role === 'leave' ? ' disabled' : '';
        var minSz = row.kind === 'new' ? 1 : (row.currentGb || 1);
        html.push(
          '<tr data-planner-idx="' + idx + '">' +
          '<td><input type="checkbox" class="form-check-input planner-select"' +
            (row.selected ? ' checked' : '') +
            (row.kind === 'new' ? ' disabled' : '') + '></td>' +
          '<td><code>' + keyLabel + '</code>' + serialBit +
            (row.kind === 'new'
              ? ' <button type="button" class="btn btn-link btn-sm planner-remove p-0">remove</button>'
              : '') +
            '</td>' +
          '<td>' + currentLabel + '</td>' +
          '<td><select class="form-select form-select-sm planner-role">' + roleOpts + '</select></td>' +
          '<td><input type="number" class="form-control form-control-sm planner-target" min="' +
            minSz + '" value="' + (row.targetGb || minSz) + '"' + disabledSize + '></td>' +
          '<td><input type="text" class="form-control form-control-sm planner-letter" maxlength="1" value="' +
            (row.letter || '') + '"' + disabledSize + '></td></tr>'
        );
      });
      bodyEl.innerHTML = html.join('');
      updateSwapEnabled();
    }

    function updateSwapEnabled() {
      if (!swapBtn) return;
      var selected = state.rows.filter(function (r) {
        return r.kind === 'existing' && r.selected;
      });
      swapBtn.disabled = selected.length !== 2;
    }

    function ensureRoleLetters() {
      var used = [];
      state.rows.forEach(function (row) {
        if (row.role === 'data' || row.role === 'pagefile') {
          if (!row.letter || used.indexOf(row.letter) !== -1) {
            row.letter = defaultLetter(row.role, used);
          }
          used.push(row.letter);
        }
      });
    }

    async function loadInventory() {
      if (!templateInput || !templateInput.value) {
        setStatus('No template selected.');
        return;
      }
      if (state.loading) return;
      state.loading = true;
      setStatus('Loading template disks…');
      try {
        var qs = new URLSearchParams();
        if (remoteInput && remoteInput.value) qs.set('remote_id', remoteInput.value);
        var url = '/api/templates/' + encodeURIComponent(templateInput.value) + '/disks';
        if (qs.toString()) url += '?' + qs.toString();
        var res = await fetch(url, { headers: { Accept: 'application/json' } });
        var body = {};
        try { body = await res.json(); } catch (e) { body = {}; }
        if (!res.ok) {
          setStatus(body.error || 'Could not load template disks.');
          state.loaded = false;
          return;
        }
        state.inventory = body.disks || [];
        rebuildRowsFromInventory(state.inventory);
        ensureRoleLetters();
        state.loaded = true;
        setStatus(
          'Template disks loaded. Assign roles, grow sizes if needed, then continue. ' +
          'GuestOS never shrinks disks.'
        );
        render();
      } catch (e) {
        setStatus('Could not load template disks.');
        state.loaded = false;
      } finally {
        state.loading = false;
      }
    }

    function onBodyChange(ev) {
      var tr = ev.target.closest('tr[data-planner-idx]');
      if (!tr) return;
      var idx = parseInt(tr.getAttribute('data-planner-idx'), 10);
      var row = state.rows[idx];
      if (!row) return;
      if (ev.target.classList.contains('planner-remove')) {
        state.rows.splice(idx, 1);
        render();
        return;
      }
      readDomIntoState();
      if (ev.target.classList.contains('planner-role')) {
        if (row.role === 'leave') {
          row.letter = '';
        } else {
          ensureRoleLetters();
          if (!row.targetGb || row.targetGb < (row.currentGb || 1)) {
            row.targetGb = row.currentGb || row.targetGb || 16;
          }
        }
        // Enforce at most one pagefile.
        if (row.role === 'pagefile') {
          state.rows.forEach(function (r, i) {
            if (i !== idx && r.role === 'pagefile') {
              r.role = 'leave';
              r.letter = '';
            }
          });
        }
        ensureRoleLetters();
        render();
      }
      updateSwapEnabled();
    }

    function addNewDisk(role) {
      readDomIntoState();
      var hasPf = state.rows.some(function (r) { return r.role === 'pagefile'; });
      if (role === 'pagefile' && hasPf) {
        setStatus('Only one pagefile disk is allowed; change the existing one or leave it.');
        return;
      }
      var used = state.rows
        .filter(function (r) { return r.letter; })
        .map(function (r) { return r.letter; });
      var size = role === 'pagefile' ? 16 : 50;
      state.rows.push({
        kind: 'new',
        key: '',
        currentGb: 0,
        role: role,
        targetGb: size,
        letter: defaultLetter(role, used),
        selected: false,
        serial: '',
      });
      render();
    }

    function swapSelected() {
      readDomIntoState();
      var idxs = [];
      state.rows.forEach(function (r, i) {
        if (r.kind === 'existing' && r.selected) idxs.push(i);
      });
      if (idxs.length !== 2) return;
      var a = state.rows[idxs[0]];
      var b = state.rows[idxs[1]];
      var tmpRole = a.role;
      var tmpLetter = a.letter;
      a.role = b.role;
      a.letter = b.letter;
      b.role = tmpRole;
      b.letter = tmpLetter;
      a.selected = false;
      b.selected = false;
      ensureRoleLetters();
      render();
    }

    function applyVisibility() {
      var on = manageCb && manageCb.checked;
      setSectionVisible(diskFields, on);
      if (on && !state.loaded && !state.loading) {
        loadInventory();
      }
    }

    if (bodyEl) {
      bodyEl.addEventListener('change', onBodyChange);
      bodyEl.addEventListener('click', function (ev) {
        if (ev.target.classList.contains('planner-remove')) onBodyChange(ev);
      });
    }
    if (swapBtn) swapBtn.addEventListener('click', swapSelected);
    if (addPfBtn) addPfBtn.addEventListener('click', function () { addNewDisk('pagefile'); });
    if (addDataBtn) addDataBtn.addEventListener('click', function () { addNewDisk('data'); });
    if (reloadBtn) {
      reloadBtn.addEventListener('click', function () {
        state.loaded = false;
        loadInventory();
      });
    }
    if (manageCb) manageCb.addEventListener('change', applyVisibility);
    applyVisibility();
    return { apply: applyVisibility, loadInventory: loadInventory, getState: function () { return state; } };
  }

  function collectDiskPlanFromPlanner(form) {
    var state = form._diskPlanner;
    if (!state || !state.rows || !state.rows.length) return null;
    // Sync latest DOM values.
    var bodyEl = form.querySelector('#disk_planner_body');
    if (bodyEl) {
      bodyEl.querySelectorAll('tr[data-planner-idx]').forEach(function (tr) {
        var idx = parseInt(tr.getAttribute('data-planner-idx'), 10);
        var row = state.rows[idx];
        if (!row) return;
        var roleEl = tr.querySelector('.planner-role');
        var targetEl = tr.querySelector('.planner-target');
        var letterEl = tr.querySelector('.planner-letter');
        if (roleEl && row.kind !== 'boot') row.role = roleEl.value;
        if (targetEl) {
          var t = parseInt(targetEl.value, 10);
          if (!isNaN(t)) row.targetGb = t;
        }
        if (letterEl && row.kind !== 'boot') {
          row.letter = (letterEl.value || '').toUpperCase().slice(0, 1);
        }
      });
    }
    var disks = [];
    var boot = state.rows.filter(function (r) { return r.kind === 'boot'; })[0];
    var osEntry = { role: 'os' };
    if (boot) {
      osEntry.source_key = boot.key;
      if (boot.targetGb && boot.currentGb && boot.targetGb > boot.currentGb) {
        osEntry.grow_to_gb = boot.targetGb;
      }
    }
    disks.push(osEntry);
    state.rows.forEach(function (row) {
      if (row.kind === 'boot') return;
      if (row.role !== 'data' && row.role !== 'pagefile') return;
      var entry = {
        role: row.role,
        size_gb: parseInt(row.targetGb, 10) || 1,
        drive_letter: row.letter || (row.role === 'pagefile' ? 'P' : 'D'),
      };
      if (row.kind === 'existing' && row.key) entry.source_key = row.key;
      if (row.role === 'pagefile') entry.ensure_pagefile = true;
      if (row.role === 'data') entry.label = 'Data';
      disks.push(entry);
    });
    return disks;
  }

  function collectSysprepPayload(form) {
    // Bulk Domain-step profile select has no name=; sync into #domain_profile for FormData.
    var profileJoin = form.querySelector('#domain_profile_join');
    var profileNet = form.querySelector('#domain_profile');
    if (profileJoin && profileNet && (profileJoin.value || '').trim()) {
      profileNet.value = profileJoin.value;
    }
    var data = Object.fromEntries(new FormData(form).entries());
    var joinDomain = !!(form.querySelector('#join_domain_checkbox') || {}).checked;
    var useProfileCreds = !!(form.querySelector('#use_domain_profile_credentials') || {}).checked;
    data.join_domain = joinDomain;
    data.use_domain_profile_credentials = useProfileCreds;
    if (profileNet && (profileNet.value || '').trim()) {
      data.domain_profile = profileNet.value.trim();
    }
    data.enable_ipv6 = !!(form.querySelector('#enable_ipv6') || {}).checked;
    if (!data.enable_ipv6) {
      data.ipv6_address = '';
      data.ipv6_prefix = '';
      data.ipv6_gateway = '';
    }
    if (!joinDomain || useProfileCreds) {
      data.domain_name = '';
      data.domain_username = '';
      data.domain_password = '';
    } else {
      var pw = data.domain_password || '';
      var pwConfirmEl = form.querySelector('#domain_password_confirm');
      var pwConfirm = pwConfirmEl ? (pwConfirmEl.value || '') : '';
      if (pw !== pwConfirm) {
        if (pwConfirmEl && window.GuestOSValidate) {
          GuestOSValidate.setInvalid(pwConfirmEl, 'Passwords do not match.');
        }
        throw new Error('Domain password and confirmation do not match.');
      }
    }
    if (joinDomain) {
      data.workgroup = '';
    }

    // UI collects RAM in GB; API / Proxmox expect MB.
    var ramGbRaw = data.ram_gb;
    delete data.ram_gb;
    var ramGb = parseFloat(ramGbRaw);
    if (!isNaN(ramGb) && ramGb > 0) {
      data.ram = Math.round(ramGb * 1024);
    }

    var applySpec = form.querySelector('#apply_spec');
    if (applySpec && applySpec.value) {
      data.spec_id = parseInt(applySpec.value, 10);
    }

    var extra = collectExtraNics(form);
    if (extra.length) {
      var primary = {
        bridge: data.bridge,
        vlan: data.vlan,
        network_mode: data.network_mode || 'static',
        ip_address: data.ip_address,
        netmask_cidr: data.netmask_cidr,
        gateway: data.gateway,
        dns_servers: data.dns_servers,
        enable_ipv6: data.enable_ipv6,
        ipv6_address: data.ipv6_address,
        ipv6_prefix: data.ipv6_prefix,
        ipv6_gateway: data.ipv6_gateway,
      };
      data.nics = [primary].concat(extra);
    }

    var disksPanel = form.querySelector('#disks-wizard-panel');
    var disksDisabled = disksPanel && disksPanel.getAttribute('data-wizard-disabled') === 'true';
    var manageDisks = !disksDisabled && !!(form.querySelector('#manage_disks_checkbox') || {}).checked;
    data.manage_disks = manageDisks;
    delete data.os_grow_to_gb;
    delete data.pagefile_size_gb;
    delete data.pagefile_drive_letter;
    delete data.data_size_gb;
    delete data.data_drive_letter;
    if (manageDisks) {
      var disks = collectDiskPlanFromPlanner(form);
      data.disks = disks && disks.length ? disks : [{ role: 'os' }];
    } else {
      data.disks = [];
    }
    return data;
  }
  function isUsableHostIPv4(ip) {
    var parts = String(ip || '').trim().split('.');
    if (parts.length !== 4) return false;
    var octets = [];
    for (var i = 0; i < 4; i++) {
      if (!/^\d+$/.test(parts[i])) return false;
      var n = Number(parts[i]);
      if (n < 0 || n > 255) return false;
      octets.push(n);
    }
    if (octets[0] === 0) return false; // 0.0.0.0/8
    if (octets[0] === 127) return false; // loopback
    if (octets[0] === 169 && octets[1] === 254) return false; // link-local
    if (octets[0] >= 224) return false; // multicast / reserved
    return true;
  }

  function isValidBulkHostname(name) {
    var label = String(name || '').trim().split('.')[0];
    return /^[A-Za-z0-9-]{1,15}$/.test(label) && !/^-/.test(label) && !/-$/.test(label);
  }

  function parseBulkRows(text, mode) {
    var lines = (text || '').split(/\r?\n/);
    var rows = [];
    var isDhcp = mode === 'dhcp';
    var inferredPrefix = '';
    var seenHost = {};
    var seenIp = {};
    var errors = [];

    function parseIpWithOptionalPrefix(value) {
      var raw = (value || '').trim();
      if (!raw) return { ip: '', prefix: '' };
      var parts = raw.split('/');
      var ip = (parts[0] || '').trim();
      var prefix = (parts[1] || '').trim();
      return { ip: ip, prefix: prefix };
    }

    lines.forEach(function (line, idx) {
      var lineNo = idx + 1;
      var raw = (line || '').trim();
      if (!raw) return;
      var parts = raw.split(',').map(function (p) { return p.trim(); });
      var hostname = parts[0] || '';
      if (!hostname) {
        errors.push('Line ' + lineNo + ': hostname is required.');
        return;
      }
      if (!isValidBulkHostname(hostname)) {
        errors.push(
          'Line ' + lineNo + ': invalid hostname "' + hostname +
            '" (1–15 letters/digits/hyphen).'
        );
        return;
      }
      var hostKey = hostname.toLowerCase();
      if (seenHost[hostKey]) {
        errors.push(
          'Duplicate hostname "' + hostname + '" on lines ' +
            seenHost[hostKey] + ' and ' + lineNo + '.'
        );
        return;
      }
      seenHost[hostKey] = lineNo;

      var row = { hostname: hostname.split('.')[0] };
      if (isDhcp) {
        if (parts[1]) {
          var vlanDhcp = parseInt(parts[1], 10);
          if (!Number.isInteger(vlanDhcp) || vlanDhcp < 1 || vlanDhcp > 4094) {
            errors.push('Line ' + lineNo + ': VLAN must be 1–4094.');
            return;
          }
          row.vlan = String(vlanDhcp);
        }
      } else {
        if (!parts[1]) {
          errors.push('Line ' + lineNo + ': static mode requires ip/prefix.');
          return;
        }
        var parsed = parseIpWithOptionalPrefix(parts[1]);
        if (!parsed.ip || !isUsableHostIPv4(parsed.ip)) {
          errors.push(
            'Line ' + lineNo + ': invalid or unusable IP "' + (parsed.ip || parts[1]) +
              '" (no loopback/link-local/multicast).'
          );
          return;
        }
        if (seenIp[parsed.ip]) {
          errors.push(
            'Duplicate IP ' + parsed.ip + ' on lines ' +
              seenIp[parsed.ip] + ' and ' + lineNo + '.'
          );
          return;
        }
        seenIp[parsed.ip] = lineNo;
        row.ip_address = parsed.ip;
        if (!parsed.prefix) {
          errors.push('Line ' + lineNo + ': CIDR prefix required (e.g. ' + parsed.ip + '/24).');
          return;
        }
        if (!/^\d+$/.test(parsed.prefix)) {
          errors.push('Line ' + lineNo + ': CIDR prefix must look like /24.');
          return;
        }
        var prefixInt = parseInt(parsed.prefix, 10);
        if (prefixInt < 1 || prefixInt > 32) {
          errors.push('Line ' + lineNo + ': CIDR must be between /1 and /32.');
          return;
        }
        if (!inferredPrefix) inferredPrefix = String(prefixInt);
        if (inferredPrefix !== String(prefixInt)) {
          errors.push(
            'Line ' + lineNo + ': all rows must use the same CIDR prefix (/' +
              inferredPrefix + ').'
          );
          return;
        }
        if (parts[2]) {
          var vlanStatic = parseInt(parts[2], 10);
          if (!Number.isInteger(vlanStatic) || vlanStatic < 1 || vlanStatic > 4094) {
            errors.push('Line ' + lineNo + ': VLAN must be 1–4094.');
            return;
          }
          row.vlan = String(vlanStatic);
        }
      }
      rows.push(row);
    });

    if (errors.length) {
      var err = new Error(errors.slice(0, 5).join(' '));
      err.bulkErrors = errors;
      throw err;
    }
    return { rows: rows, prefix: inferredPrefix, errors: [] };
  }

  function validateBulkCsvText(text, mode) {
    try {
      return { ok: true, parsed: parseBulkRows(text, mode), errors: [] };
    } catch (e) {
      return {
        ok: false,
        parsed: null,
        errors: e.bulkErrors || [e.message || 'Invalid bulk CSV.'],
      };
    }
  }

  function estimateRequestedDiskGb(form) {
    var manage = !!(form.querySelector('#manage_disks_checkbox') || {}).checked;
    if (!manage) return 0;
    var disks = collectDiskPlanFromPlanner(form);
    if (!disks || !disks.length) return 0;
    var total = 0;
    disks.forEach(function (d) {
      if (d.role === 'os') {
        if (d.grow_to_gb) total += parseInt(d.grow_to_gb, 10) || 0;
      } else if (d.size_gb) {
        total += parseInt(d.size_gb, 10) || 0;
      }
    });
    return total;
  }

  function estimateBulkDiskSumGb(form, itemCount) {
    var perVm = estimateRequestedDiskGb(form);
    var n = Math.max(0, parseInt(itemCount, 10) || 0);
    return { per_vm_gb: perVm, items: n, batch_sum_gb: perVm * n };
  }

  function collectBulkPayload(form) {
    var shared = collectSysprepPayload(form);
    var modeEl = form.querySelector('#bulk_network_input_mode');
    var mode = modeEl && modeEl.value ? modeEl.value : 'static';
    var rowsText = (form.querySelector('#bulk_rows') || {}).value || '';
    var parsed = parseBulkRows(rowsText, mode);
    var rows = parsed.rows;
    var bulkGateway = (form.querySelector('#bulk_gateway') || {}).value || '';
    var bulkDns = (form.querySelector('#bulk_dns_servers') || {}).value || '';
    // In bulk mode the per-row list owns hostnames/IPs — keep domain_profile.
    shared.hostname = '';
    shared.ip_address = '';
    shared.vlan = '';
    shared.dns_servers = bulkDns.trim();
    if (mode === 'dhcp') {
      shared.network_mode = 'dhcp';
      shared.gateway = '';
      shared.netmask_cidr = '';
    } else {
      shared.network_mode = 'static';
      shared.gateway = bulkGateway.trim();
      shared.netmask_cidr = parsed.prefix || shared.netmask_cidr || '24';
      if (!shared.gateway) {
        throw new Error('Bulk static mode requires a default gateway.');
      }
      if (!isUsableHostIPv4(shared.gateway)) {
        throw new Error('Default gateway must be a usable IPv4 address.');
      }
      rows.forEach(function (r) {
        if (r.ip_address && r.ip_address === shared.gateway) {
          throw new Error(
            'Default gateway ' + shared.gateway +
              ' collides with hostname ' + r.hostname + ' IP.'
          );
        }
      });
    }
    return {
      shared: shared,
      items: rows,
    };
  }

  function fillReview(form, target) {
    if (!target) return;
    var htmlRows = [];
    function add(label, value) {
      htmlRows.push('<dt>' + label + '</dt><dd>' + (value || '—') + '</dd>');
    }
    var provisionMode = form.querySelector('#provision_mode');
    var isBulk = provisionMode && provisionMode.value === 'bulk';
    var hostname = form.querySelector('#hostname');
    if (!isBulk && hostname) add('Hostname', hostname.value);
    if (isBulk) {
      var bulkMode = (form.querySelector('#bulk_network_input_mode') || {}).value || 'static';
      var bulkCheck = validateBulkCsvText(
        (form.querySelector('#bulk_rows') || {}).value || '',
        bulkMode
      );
      if (!bulkCheck.ok) {
        add('CSV validation', bulkCheck.errors.slice(0, 3).join(' '));
      } else {
        var parsed = bulkCheck.parsed;
        var desktopRows = parsed.rows || [];
        add('Provision mode', 'Bulk');
        add('Desktop rows', String(desktopRows.length));
        add('Row network mode', bulkMode);
        if (bulkMode === 'static') {
          add('CIDR prefix', parsed.prefix || (form.querySelector('#netmask_cidr') || {}).value);
          add('Gateway', (form.querySelector('#bulk_gateway') || {}).value);
        }
        add('DNS', (form.querySelector('#bulk_dns_servers') || {}).value);
        var hostnames = desktopRows.map(function (r) { return r.hostname; }).filter(Boolean);
        if (hostnames.length) {
          add('Hostnames', hostnames.join(', '));
        }
        var diskEst = estimateBulkDiskSumGb(form, desktopRows.length);
        if (diskEst.per_vm_gb > 0) {
          add(
            'Requested disks (info)',
            diskEst.per_vm_gb + ' GB × ' + diskEst.items +
              ' = ' + diskEst.batch_sum_gb + ' GB (display only; not enforced)'
          );
        } else {
          add('Requested disks (info)', 'None (Configure disks off / thin clone)');
        }
      }
    } else {
      add('Provision mode', 'Single');
      var singleDisk = estimateRequestedDiskGb(form);
      if (singleDisk > 0) add('Requested disks (info)', singleDisk + ' GB');
    }
    var remote = form.querySelector('#remote_id, [name="remote_id"]');
    if (remote && remote.value) add('Remote', remote.value);
    var cores = form.querySelector('#cores');
    if (cores) add('CPU cores', cores.value);
    var ram = form.querySelector('#ram_gb');
    if (ram) add('RAM', ram.value + ' GB');
    var bridge = form.querySelector('#bridge');
    if (bridge) add('Bridge', bridge.value);
    if (!isBulk) {
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
      var profileName = (form.querySelector('#domain_profile') || {}).value;
      if (profileName) add('Network profile', profileName);
    }
    var join = form.querySelector('#join_domain_checkbox');
    if (join) add('Join domain', join.checked ? 'Yes' : 'No');
    if (join && join.checked) {
      var useProf = form.querySelector('#use_domain_profile_credentials');
      if (useProf) {
        add(
          'Domain credentials',
          useProf.checked ? 'From network profile' : 'Manual'
        );
      }
      if (form.dataset.domainCredTest === 'failed') {
        add(
          'Credential test',
          'Not validated from GuestOS host (you can still submit; in-clone probe runs later)'
        );
      } else if (form.dataset.domainCredTest === 'ok') {
        add('Credential test', 'OK (host LDAP bind)');
      }
    }
    if (join && !join.checked) {
      add('Workgroup', (form.querySelector('#workgroup') || {}).value);
    }
    add('Timezone', (form.querySelector('#timezone') || {}).value);
    add('Locale', (form.querySelector('#locale') || {}).value);
    var ipv6 = form.querySelector('#enable_ipv6');
    if (ipv6) {
      add('IPv6', ipv6.checked ? 'Enabled' : 'Off');
      if (ipv6.checked) {
        add('IPv6 address', (form.querySelector('#ipv6_address') || {}).value);
      }
    }
    var extras = form.querySelectorAll('.extra-nic');
    if (extras.length) add('Additional NICs', String(extras.length));
    var manageDisks = form.querySelector('#manage_disks_checkbox');
    if (manageDisks) {
      add('Configure disks', manageDisks.checked ? 'Yes' : 'No');
      if (manageDisks.checked) {
        var plan = collectDiskPlanFromPlanner(form) || [];
        plan.forEach(function (d) {
          if (d.role === 'os') {
            add(
              'OS disk',
              (d.source_key ? d.source_key + ' ' : '') +
                (d.grow_to_gb ? ('grow to ' + d.grow_to_gb + 'G') : 'keep size')
            );
          } else {
            add(
              d.role + ' disk',
              (d.source_key ? d.source_key + ' → ' : 'new → ') +
                d.size_gb + 'G ' + (d.drive_letter || '') + ':'
            );
          }
        });
      }
    }
    target.innerHTML = htmlRows.join('');
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
    wireDomainProfileNetwork: wireDomainProfileNetwork,
    wireNetworkMode: wireNetworkMode,
    wireDomainJoin: wireDomainJoin,
    wireManageDisks: wireManageDisks,
    wireIpv6: wireIpv6,
    wireExtraNics: wireExtraNics,
    collectExtraNics: collectExtraNics,
    wireApplySpec: wireApplySpec,
    applySpecPayload: applySpecPayload,
    collectSysprepPayload: collectSysprepPayload,
    collectBulkPayload: collectBulkPayload,
    parseBulkRows: parseBulkRows,
    validateBulkCsvText: validateBulkCsvText,
    fillReview: fillReview,
    estimateRequestedDiskGb: estimateRequestedDiskGb,
    estimateBulkDiskSumGb: estimateBulkDiskSumGb,
    postJson: postJson,
    mapServerErrors: mapServerErrors,
  };
})(window);
